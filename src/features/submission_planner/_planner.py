"""Polarity lock, per-market cap, 3-item cap, and deterministic Idempotency-Key planner."""

from __future__ import annotations

import hashlib
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from src.kernel.event_bus import ImmutableEventBus

COMPETITION_MIN_SUBMISSIONS = 10
COMPETITION_MIN_MARKETS = 3
MAX_TOTAL_ITEMS = 3
MAX_NOTE_CHARS = 8000
PER_MARKET_CAP_BY_TIER: dict[str, int] = {
    "restricted": 1,
    "standard": 3,
}
BASIC_FETCH_FRAGILE_DOMAINS: frozenset[str] = frozenset({
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "reddit.com",
    "www.reddit.com",
    "wsj.com",
    "www.wsj.com",
    "ft.com",
    "www.ft.com",
    "bloomberg.com",
    "www.bloomberg.com",
})


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    market_address: str
    market_question: str
    polarity: str
    note: str
    links: tuple[str, ...]
    files: tuple[str, ...] = ()
    verbatim_quote: str = ""
    source_text: str = ""
    kl_divergence_nats: float = 0.0


@dataclass(frozen=True, slots=True)
class PlannedSubmission:
    submission_id: str
    idempotency_key: str
    market_address: str
    market_question: str
    directional_polarity: str
    slot_index: int
    note: str
    links: tuple[str, ...]
    files: tuple[str, ...]
    total_items: int
    fragile_domain_flag: bool
    request_headers: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "idempotency_key": self.idempotency_key,
            "market_address": self.market_address,
            "market_question": self.market_question,
            "directional_polarity": self.directional_polarity,
            "slot_index": self.slot_index,
            "note": self.note,
            "links": list(self.links),
            "files": list(self.files),
            "total_items": self.total_items,
            "fragile_domain_flag": self.fragile_domain_flag,
            "request_headers": dict(self.request_headers),
        }


@dataclass(frozen=True, slots=True)
class SubmissionPlan:
    generated_at_utc: str
    tier: str
    per_market_cap: int
    total_submissions: int
    distinct_markets: int
    meets_competition_minimums: bool
    submissions: tuple[PlannedSubmission, ...]
    rejections: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at_utc": self.generated_at_utc,
            "tier": self.tier,
            "per_market_cap": self.per_market_cap,
            "total_submissions": self.total_submissions,
            "distinct_markets": self.distinct_markets,
            "meets_competition_minimums": self.meets_competition_minimums,
            "submissions": [s.to_dict() for s in self.submissions],
            "rejections": list(self.rejections),
        }


def compute_idempotency_key(
    agent_id: str,
    market_address: str,
    slot_index: int,
    links: tuple[str, ...],
    note: str,
) -> str:
    canonical = f"{agent_id.strip()}|{market_address.lower()}|slot-{slot_index}|{'|'.join(links)}|{note}"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"delphi-ev-{digest}"


def verify_verbatim_quote(quote: str, source_text: str) -> bool:
    if not quote or not source_text:
        return True
    return quote in source_text


def build_submission_plan(
    candidates: Iterable[EvidenceCandidate],
    *,
    tier: str = "standard",
    agent_id: str = "hindsight-delphi",
    agent_harness: str = "antigravity-vertical-slice",
    agent_model: str = "gemini-4-neon-medium",
    min_submissions: int = COMPETITION_MIN_SUBMISSIONS,
    min_markets: int = COMPETITION_MIN_MARKETS,
    strict_reject: bool = False,
) -> SubmissionPlan:
    """Validate and order evidence candidates under Delphi competition guardrails."""
    if tier not in PER_MARKET_CAP_BY_TIER:
        raise ValueError(f"Unsupported tier {tier!r}; expected one of {sorted(PER_MARKET_CAP_BY_TIER)}")
    per_market_cap = PER_MARKET_CAP_BY_TIER[tier]

    ordered = sorted(candidates, key=lambda c: (-c.kl_divergence_nats, c.market_address.lower()))
    planned: list[PlannedSubmission] = []
    rejections: list[str] = []
    market_counts: dict[str, int] = {}
    market_polarity: dict[str, str] = {}
    seen_fingerprints: set[tuple[str, tuple[str, ...], str]] = set()

    for cand in ordered:
        mkt = cand.market_address.strip()
        mkt_key = mkt.lower()
        pol = cand.polarity.strip().upper()
        if pol not in ("YES", "NO"):
            msg = f"Invalid polarity {cand.polarity!r} for {mkt}"
            if strict_reject:
                raise ValueError(msg)
            rejections.append(msg)
            continue

        has_note = bool(cand.note and cand.note.strip())
        if has_note and len(cand.note) > MAX_NOTE_CHARS:
            msg = f"Note exceeds {MAX_NOTE_CHARS} chars for {mkt}"
            if strict_reject:
                raise ValueError(msg)
            rejections.append(msg)
            continue

        if any(not url.startswith("https://") for url in cand.links):
            msg = f"Non-https link rejected for {mkt}"
            if strict_reject:
                raise ValueError(msg)
            rejections.append(msg)
            continue

        total_items = (1 if has_note else 0) + len(cand.links) + len(cand.files)
        if total_items < 1 or total_items > MAX_TOTAL_ITEMS:
            msg = f"Total items {total_items} outside 1..{MAX_TOTAL_ITEMS} for {mkt}"
            if strict_reject:
                raise ValueError(msg)
            rejections.append(msg)
            continue

        if cand.verbatim_quote and not verify_verbatim_quote(cand.verbatim_quote, cand.source_text):
            msg = f"Verbatim quote not found in source text for {mkt}"
            if strict_reject:
                raise ValueError(msg)
            rejections.append(msg)
            continue

        fingerprint = (mkt_key, cand.links, cand.note.strip())
        if fingerprint in seen_fingerprints:
            rejections.append(f"Duplicate submission skipped for {mkt}")
            continue

        locked = market_polarity.get(mkt_key)
        if locked is not None and locked != pol:
            msg = f"Opposing polarity {pol} blocked against locked {locked} on {mkt}"
            if strict_reject:
                raise ValueError(msg)
            rejections.append(msg)
            continue

        count = market_counts.get(mkt_key, 0)
        if count >= per_market_cap:
            msg = f"Per-market cap ({per_market_cap}) reached for {mkt}"
            if strict_reject:
                raise ValueError(msg)
            rejections.append(msg)
            continue

        slot = count + 1
        market_counts[mkt_key] = slot
        market_polarity[mkt_key] = pol
        seen_fingerprints.add(fingerprint)

        idem_key = compute_idempotency_key(agent_id, mkt, slot, cand.links, cand.note)
        sub_id = hashlib.sha256(f"{mkt_key}|{slot}|{idem_key}".encode("utf-8")).hexdigest()[:16]
        fragile = any((urllib.parse.urlparse(u).hostname or "").lower() in BASIC_FETCH_FRAGILE_DOMAINS for u in cand.links)

        planned.append(
            PlannedSubmission(
                submission_id=sub_id,
                idempotency_key=idem_key,
                market_address=mkt,
                market_question=cand.market_question,
                directional_polarity=pol,
                slot_index=slot,
                note=cand.note,
                links=cand.links,
                files=cand.files,
                total_items=total_items,
                fragile_domain_flag=fragile,
                request_headers={
                    "Idempotency-Key": idem_key,
                    "X-Agent-Id": agent_id,
                    "X-Agent-Harness": agent_harness,
                    "X-Agent-Model": agent_model,
                },
            )
        )

    distinct_mkts = len({s.market_address.lower() for s in planned})
    meets = len(planned) >= min_submissions and distinct_mkts >= min_markets
    return SubmissionPlan(
        generated_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        tier=tier,
        per_market_cap=per_market_cap,
        total_submissions=len(planned),
        distinct_markets=distinct_mkts,
        meets_competition_minimums=meets,
        submissions=tuple(planned),
        rejections=tuple(rejections),
    )


def register(bus: ImmutableEventBus, *, tier: str = "standard") -> None:
    """Subscribe to ``plan.build.requested`` and emit ``plan.built``."""

    def _on_build(event: Any) -> None:
        raw_cands = event.payload.get("candidates", ())
        cands = [
            EvidenceCandidate(
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
            for c in raw_cands
        ]
        plan = build_submission_plan(cands, tier=tier)
        bus.emit(
            "plan.built",
            plan.to_dict(),
            source_brick="submission_planner",
            correlation_id=event.correlation_id,
        )

    bus.subscribe("plan.build.requested", _on_build)
