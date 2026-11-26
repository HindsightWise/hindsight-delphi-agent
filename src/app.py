"""Thin top-level orchestrator for Hindsight-Delphi (DoraHacks #2479)."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from src.features import competition_markets, evidence_api, submission_planner
from src.kernel.event_bus import ImmutableEventBus, unfreeze_value

DEFAULT_AGENT_ID = "hindsight-delphi"
DEFAULT_HARNESS = "antigravity-vertical-slice"
DEFAULT_MODEL = "gemini-4-neon-medium"
BOUND_WALLET = "0x7789f243fd6d569586354a9dd66c0b105f77168b"


def _resolve_identity() -> evidence_api.AgentIdentity:
    return evidence_api.AgentIdentity(
        agent_id=os.environ.get("DELPHI_AGENT_ID") or DEFAULT_AGENT_ID,
        agent_harness=os.environ.get("DELPHI_AGENT_HARNESS") or os.environ.get("DELPHI_HARNESS") or DEFAULT_HARNESS,
        agent_model=os.environ.get("DELPHI_AGENT_MODEL") or os.environ.get("DELPHI_MODEL") or DEFAULT_MODEL,
    )


def run_dry_run_scan(
    *,
    markets_file: str | Path | None = None,
    limit: int = 12,
    tier: str = "standard",
) -> dict[str, Any]:
    """Load & rank live/ingested competition markets and emit a zero-write dry-run summary."""
    bus = ImmutableEventBus()
    cid = str(uuid.uuid4())
    identity = _resolve_identity()
    competition_markets.register(bus)
    submission_planner.register(bus, tier=tier)

    bus.emit(
        "markets.load.requested",
        {"markets_file": str(markets_file) if markets_file else None, "limit": limit, "only_open": True},
        source_brick="app",
        correlation_id=cid,
    )
    loaded = bus.history("markets.loaded")
    markets_payload = unfreeze_value(loaded[-1].payload) if loaded else {"count": 0, "markets": []}

    return {
        "correlation_id": cid,
        "mode": "dry_run",
        "wallet": BOUND_WALLET,
        "agent_id": identity.agent_id,
        "agent_harness": identity.agent_harness,
        "agent_model": identity.agent_model,
        "evidence_api_base": evidence_api.DEFAULT_BASE_URL,
        "event_bus_topics": [ev.topic for ev in bus.history()],
        "markets_loaded": markets_payload,
        "live_writes_executed": 0,
    }


def check_live_access(api_key: str) -> dict[str, Any]:
    """Read-only preflight check against GET /v1/limits and GET /v1/terms."""
    client = evidence_api.EvidenceApiClient(api_key, identity=_resolve_identity())
    limits = client.get_limits()
    terms = client.get_terms()
    return {"limits": limits, "terms": terms}


def run_evidence_cycle(
    candidates_file: str | Path,
    *,
    execute_live: bool = False,
    api_key: str | None = None,
    tier: str = "standard",
    client: Any | None = None,
) -> dict[str, Any]:
    """Build a validated submission plan from candidates JSON and optionally execute live writes.

    Never auto-accepts Delphi Terms of Service; requires human acceptance when terms.acceptedAt is null.
    """
    raw_items = json.loads(Path(candidates_file).read_text(encoding="utf-8"))
    if isinstance(raw_items, dict):
        raw_items = raw_items.get("candidates", [])
    identity = _resolve_identity()
    candidates = [
        submission_planner.EvidenceCandidate(
            market_address=str(c["market_address"]),
            market_question=str(c.get("market_question", "")),
            polarity=str(c.get("polarity", "YES")),
            note=str(c.get("note", "")),
            links=tuple(str(u) for u in c.get("links", ())),
            files=tuple(str(f) for f in c.get("files", ())),
            verbatim_quote=str(c.get("verbatim_quote", "")),
            source_text=str(c.get("source_text", "")),
            kl_divergence_nats=float(c.get("kl_divergence_nats", 0.0)),
        )
        for c in raw_items
    ]
    plan = submission_planner.build_submission_plan(
        candidates,
        tier=tier,
        agent_id=identity.agent_id,
        agent_harness=identity.agent_harness,
        agent_model=identity.agent_model,
    )

    if not execute_live:
        return {
            "mode": "dry_run",
            "wallet": BOUND_WALLET,
            "plan": plan.to_dict(),
            "receipts": [],
            "skipped": [],
            "live_writes_executed": 0,
        }

    api_client = client
    if api_client is None:
        resolved_key = (api_key or os.environ.get("DELPHI_API_ACCESS_KEY", "")).strip()
        if not resolved_key:
            raise RuntimeError(
                "DELPHI_API_ACCESS_KEY is unset; provision a Mainnet key at https://api-access.delphi.fyi"
            )
        api_client = evidence_api.EvidenceApiClient(resolved_key, identity=identity)

    limits_resp = api_client.get_limits()
    limits_data = limits_resp.get("data", limits_resp)
    submits_left = int(limits_data.get("submitsLeftThisHour", 0))
    if submits_left <= 0:
        raise RuntimeError("Hourly submission quota exhausted (submitsLeftThisHour <= 0); retry after reset.")

    terms_resp = api_client.get_terms()
    terms_data = terms_resp.get("data", terms_resp)
    if not terms_data.get("acceptedAt"):
        raise RuntimeError(
            f"Delphi terms not yet accepted (version={terms_data.get('version')}, url={terms_data.get('url')}). "
            "Operator must review and accept terms explicitly before live submission."
        )

    receipts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    remaining_hourly = submits_left
    for sub in plan.submissions:
        if remaining_hourly <= 0:
            skipped.append({"submission_id": sub.submission_id, "reason": "hourly_quota_reached"})
            continue
        entry_resp = api_client.get_entry_state(sub.market_address)
        entry_data = entry_resp.get("data", entry_resp)
        can_submit = bool(entry_data.get("canSubmit", False))
        rem_market = int(entry_data.get("remainingOnMarket", 0))
        if not can_submit or rem_market <= 0:
            skipped.append({
                "submission_id": sub.submission_id,
                "market_address": sub.market_address,
                "reason": "market_entry_closed_or_capped",
            })
            continue

        req = evidence_api.MultipartEvidenceRequest(
            market_address=sub.market_address,
            note=sub.note,
            links=sub.links,
            files=tuple(Path(f) for f in sub.files),
            idempotency_key=sub.idempotency_key,
        )
        receipt = api_client.submit_evidence(req)
        receipts.append({"submission_id": sub.submission_id, "market_address": sub.market_address, "receipt": receipt})
        remaining_hourly -= 1

    return {
        "mode": "live",
        "wallet": BOUND_WALLET,
        "plan": plan.to_dict(),
        "receipts": receipts,
        "skipped": skipped,
        "live_writes_executed": len(receipts),
    }


__all__ = ["check_live_access", "run_dry_run_scan", "run_evidence_cycle"]
