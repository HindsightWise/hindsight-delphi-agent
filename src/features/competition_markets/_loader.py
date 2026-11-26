"""Loader and EV ranker for https://competition.delphi.fyi/markets.json."""

from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.kernel.event_bus import ImmutableEventBus

COMPETITION_MARKETS_URL = "https://competition.delphi.fyi/markets.json"
DEFAULT_USER_AGENT = "Hindsight-Delphi/1.0 (DoraHacks-2479)"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CompetitionMarket:
    address: str
    question: str
    category: str
    market_tier: str
    status: str
    yes_prob: float
    no_prob: float
    closes_at_utc: str
    evidence_cutoff_utc: str
    judgement_pot_usdc: float
    settlement_pot_usdc: float
    data_sources: tuple[str, ...]
    priority_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "question": self.question,
            "category": self.category,
            "market_tier": self.market_tier,
            "status": self.status,
            "yes_prob": self.yes_prob,
            "no_prob": self.no_prob,
            "closes_at_utc": self.closes_at_utc,
            "evidence_cutoff_utc": self.evidence_cutoff_utc,
            "judgement_pot_usdc": self.judgement_pot_usdc,
            "settlement_pot_usdc": self.settlement_pot_usdc,
            "data_sources": list(self.data_sources),
            "priority_score": self.priority_score,
        }


def _parse_iso_utc(raw: str) -> datetime:
    cleaned = raw.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _binary_entropy(p: float) -> float:
    clamped = min(max(p, 1e-6), 1.0 - 1e-6)
    return -(clamped * math.log2(clamped) + (1.0 - clamped) * math.log2(1.0 - clamped))


def _usdc_units_to_float(raw: Any) -> float:
    if raw is None or raw == "":
        return 0.0
    return round(float(int(str(raw))) / 1_000_000.0, 6)


def parse_and_rank_markets(
    raw_items: Iterable[dict[str, Any]],
    *,
    now_utc: datetime | None = None,
    only_open: bool = True,
    min_hours_to_cutoff: float = 0.25,
) -> tuple[CompetitionMarket, ...]:
    """Validate and rank competition markets by pot size and forecast uncertainty."""
    now = now_utc or datetime.now(timezone.utc)
    ranked: list[CompetitionMarket] = []

    for item in raw_items:
        address = str(item.get("address") or "").strip()
        question = str(item.get("question") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        cutoff_str = str(item.get("evidenceCutoffAt") or "").strip()
        if not address.startswith("0x") or len(address) != 42 or not question or not cutoff_str:
            continue
        if only_open and status != "open":
            continue

        cutoff_dt = _parse_iso_utc(cutoff_str)
        hours_left = (cutoff_dt - now).total_seconds() / 3600.0
        if only_open and hours_left < min_hours_to_cutoff:
            continue

        odds = item.get("odds") or {}
        yes_p = float(odds.get("yes", 0.5))
        no_p = float(odds.get("no", 1.0 - yes_p))
        pots = item.get("pots") or {}
        j_pot = _usdc_units_to_float(pots.get("judgement"))
        s_pot = _usdc_units_to_float(pots.get("settlement"))

        entropy = _binary_entropy(yes_p)
        band_bonus = 1.25 if 0.20 <= yes_p <= 0.80 else 0.75
        total_pot = max(j_pot + s_pot, 1.0)
        priority = round(total_pot * (0.4 + 0.6 * entropy) * band_bonus, 4)

        sources = tuple(
            str(src).strip()
            for src in (item.get("dataSources") or ())
            if str(src).strip().startswith("https://")
        )
        ranked.append(
            CompetitionMarket(
                address=address,
                question=question,
                category=str(item.get("category") or "general"),
                market_tier=str(item.get("marketTier") or "B"),
                status=status,
                yes_prob=round(yes_p, 6),
                no_prob=round(no_p, 6),
                closes_at_utc=str(item.get("closesAt") or ""),
                evidence_cutoff_utc=cutoff_str,
                judgement_pot_usdc=j_pot,
                settlement_pot_usdc=s_pot,
                data_sources=sources,
                priority_score=priority,
            )
        )

    ranked.sort(key=lambda m: (-m.priority_score, m.evidence_cutoff_utc, m.address.lower()))
    return tuple(ranked)


def load_competition_markets_file(
    path: str | Path,
    *,
    now_utc: datetime | None = None,
    only_open: bool = True,
) -> tuple[CompetitionMarket, ...]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else raw.get("markets", [])
    return parse_and_rank_markets(items, now_utc=now_utc, only_open=only_open)


def fetch_live_competition_markets(
    url: str = COMPETITION_MARKETS_URL,
    *,
    timeout: float = 12.0,
    now_utc: datetime | None = None,
    only_open: bool = True,
) -> tuple[CompetitionMarket, ...]:
    if not url.startswith("https://"):
        raise ValueError(f"Only https:// market feeds are allowed: {url!r}")
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": DEFAULT_USER_AGENT},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload_bytes = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(payload_bytes) > MAX_RESPONSE_BYTES:
            raise ValueError(f"Competition markets feed exceeded {MAX_RESPONSE_BYTES} byte safety cap")
        raw = json.loads(payload_bytes.decode("utf-8"))
    items = raw if isinstance(raw, list) else raw.get("markets", [])
    return parse_and_rank_markets(items, now_utc=now_utc, only_open=only_open)


def register(bus: ImmutableEventBus) -> None:
    """Subscribe to ``markets.load.requested`` and emit ``markets.loaded``."""

    def _on_load(event: Any) -> None:
        payload = event.payload
        file_path = payload.get("markets_file")
        if file_path:
            markets = load_competition_markets_file(file_path, only_open=bool(payload.get("only_open", True)))
        else:
            markets = fetch_live_competition_markets(only_open=bool(payload.get("only_open", True)))
        limit = int(payload.get("limit", len(markets)))
        selected = markets[:limit] if limit > 0 else markets
        bus.emit(
            "markets.loaded",
            {"count": len(selected), "markets": [m.to_dict() for m in selected]},
            source_brick="competition_markets",
            correlation_id=event.correlation_id,
        )

    bus.subscribe("markets.load.requested", _on_load)
