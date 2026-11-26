"""Unit tests for Delphi Evidence API v1 client, competition markets, and submission planner."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.app import run_dry_run_scan, run_evidence_cycle
from src.features.competition_markets import load_competition_markets_file
from src.features.evidence_api import (
    AgentIdentity,
    MultipartEvidenceRequest,
    build_evidence_headers,
    encode_multipart_submission,
)
from src.features.submission_planner import EvidenceCandidate, build_submission_plan

_SAMPLE_MARKETS_FIXTURE = [
    {
        "address": "0x1111111111111111111111111111111111111111",
        "question": "Synthetic Market A (high pot, balanced odds)",
        "category": "science",
        "marketTier": "A",
        "status": "open",
        "odds": {"yes": 0.50, "no": 0.50},
        "closesAt": "2099-12-31T23:59:00Z",
        "evidenceCutoffAt": "2099-12-30T23:59:00Z",
        "pots": {"judgement": "50000000", "settlement": "50000000"},
        "dataSources": ["https://example.org/source-a"],
    },
    {
        "address": "0x2222222222222222222222222222222222222222",
        "question": "Synthetic Market B (medium pot, skewed odds)",
        "category": "economics",
        "marketTier": "B",
        "status": "open",
        "odds": {"yes": 0.90, "no": 0.10},
        "closesAt": "2099-12-31T23:59:00Z",
        "evidenceCutoffAt": "2099-12-30T23:59:00Z",
        "pots": {"judgement": "10000000", "settlement": "10000000"},
        "dataSources": ["https://example.org/source-b"],
    },
    {
        "address": "0x3333333333333333333333333333333333333333",
        "question": "Synthetic Market C (closed)",
        "category": "general",
        "marketTier": "B",
        "status": "settled",
        "odds": {"yes": 0.50, "no": 0.50},
        "closesAt": "2020-01-01T00:00:00Z",
        "evidenceCutoffAt": "2020-01-01T00:00:00Z",
        "pots": {"judgement": "10000000", "settlement": "10000000"},
        "dataSources": [],
    },
]


class _StubEvidenceClient:
    """In-memory transport stub for testing execute_live guardrails without network calls."""

    def __init__(
        self,
        *,
        submits_left: int = 5,
        terms_accepted_at: str | None = "2026-11-25T18:00:00Z",
        can_submit: bool = True,
        remaining_on_market: int = 3,
    ) -> None:
        self._submits_left = submits_left
        self._terms_accepted_at = terms_accepted_at
        self._can_submit = can_submit
        self._remaining_on_market = remaining_on_market
        self.submitted: list[MultipartEvidenceRequest] = []

    def get_limits(self) -> dict[str, Any]:
        return {"status_code": 200, "data": {"submitsLeftThisHour": self._submits_left}}

    def get_terms(self) -> dict[str, Any]:
        return {
            "status_code": 200,
            "data": {"version": "2026-11-25", "url": "https://delphi.fyi/tos", "acceptedAt": self._terms_accepted_at},
        }

    def get_entry_state(self, market_address: str) -> dict[str, Any]:
        return {
            "status_code": 200,
            "data": {
                "marketAddress": market_address,
                "canSubmit": self._can_submit,
                "remainingOnMarket": self._remaining_on_market,
            },
        }

    def submit_evidence(self, req: MultipartEvidenceRequest) -> dict[str, Any]:
        self.submitted.append(req)
        return {"status_code": 202, "data": {"id": f"sub-{len(self.submitted)}", "status": "queued"}}


class TestDelphiV1EvidenceApi(unittest.TestCase):
    def test_headers_conform_to_openapi_v1_including_128_char_idempotency_key(self) -> None:
        identity = AgentIdentity(
            agent_id="hindsight-delphi",
            agent_harness="antigravity-vertical-slice",
            agent_model="gemini-4-neon-medium",
        )
        long_idem = "delphi-ev-" + ("a" * 118)
        self.assertEqual(len(long_idem), 128)
        headers = build_evidence_headers(
            "mainnet-token-sample",
            identity,
            idempotency_key=long_idem,
        )
        self.assertEqual(headers["Authorization"], "Bearer mainnet-token-sample")
        self.assertEqual(headers["X-Agent-Id"], "hindsight-delphi")
        self.assertEqual(headers["X-Agent-Harness"], "antigravity-vertical-slice")
        self.assertEqual(headers["X-Agent-Model"], "gemini-4-neon-medium")
        self.assertEqual(headers["Idempotency-Key"], long_idem)
        self.assertNotIn("python-urllib", headers["User-Agent"].lower())

        with self.assertRaises(ValueError):
            build_evidence_headers("mainnet-token-sample", identity, idempotency_key="x" * 129)

    def test_multipart_encodes_links_brackets_sanitizes_filename_and_enforces_three_item_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "proof_safe.txt"
            doc.write_text("official docket excerpt", encoding="utf-8")
            req = MultipartEvidenceRequest(
                market_address="0x94dbba7c9b44b788a701602ab6d919ff7b7fc38a",
                note="Dated primary filing from 2026-11-25.",
                links=("https://example.org/filing",),
                files=(doc,),
            )
            body, ctype = encode_multipart_submission(req, boundary="TestBoundary123")
            self.assertIn("multipart/form-data; boundary=TestBoundary123", ctype)
            self.assertIn(b'name="marketAddress"', body)
            self.assertIn(b'name="note"', body)
            self.assertIn(b'name="links[]"', body)
            self.assertIn(b'name="files"; filename="proof_safe.txt"', body)

            weird_doc = Path(tmp) / 'bad\\name"1.txt'
            weird_doc.write_text("x", encoding="utf-8")
            body2, _ = encode_multipart_submission(
                MultipartEvidenceRequest(
                    market_address="0x94dbba7c9b44b788a701602ab6d919ff7b7fc38a",
                    files=(weird_doc,),
                ),
                boundary="TestBoundary123",
            )
            self.assertIn(b'filename="bad_name_1.txt"', body2)

            too_many = MultipartEvidenceRequest(
                market_address="0x94dbba7c9b44b788a701602ab6d919ff7b7fc38a",
                note="Note item 1",
                links=("https://a.example/1", "https://b.example/2"),
                files=(doc,),
            )
            with self.assertRaises(ValueError):
                encode_multipart_submission(too_many)

    def test_rejects_non_https_and_invalid_market_address(self) -> None:
        bad_link = MultipartEvidenceRequest(
            market_address="0x94dbba7c9b44b788a701602ab6d919ff7b7fc38a",
            note="Note",
            links=("http://insecure.example/item",),
        )
        with self.assertRaises(ValueError):
            encode_multipart_submission(bad_link)

        bad_addr = MultipartEvidenceRequest(
            market_address="0x1234",
            note="Note",
        )
        with self.assertRaises(ValueError):
            encode_multipart_submission(bad_addr)


class TestSubmissionPlannerAndExecution(unittest.TestCase):
    def test_loads_and_ranks_open_competition_markets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mfile = Path(tmp) / "markets.json"
            mfile.write_text(json.dumps(_SAMPLE_MARKETS_FIXTURE), encoding="utf-8")
            now = datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)
            markets = load_competition_markets_file(mfile, now_utc=now, only_open=True)
            self.assertEqual(len(markets), 2)
            self.assertTrue(all(m.status == "open" for m in markets))
            self.assertGreater(markets[0].priority_score, markets[1].priority_score)

    def test_polarity_lock_and_per_market_cap_and_minimums(self) -> None:
        m1 = "0x94dbba7c9b44b788a701602ab6d919ff7b7fc38a"
        m2 = "0x1111111111111111111111111111111111111111"
        m3 = "0x2222222222222222222222222222222222222222"
        m4 = "0x3333333333333333333333333333333333333333"
        cands: list[EvidenceCandidate] = []
        for idx in range(3):
            cands.append(
                EvidenceCandidate(
                    market_address=m1,
                    market_question="Q1",
                    polarity="YES",
                    note=f"Fact {idx} for m1",
                    links=(f"https://example.org/m1/{idx}",),
                    kl_divergence_nats=0.5 - idx * 0.05,
                )
            )
        cands.append(
            EvidenceCandidate(
                market_address=m1,
                market_question="Q1",
                polarity="NO",
                note="Opposing fact on m1",
                links=("https://example.org/m1/oppose",),
                kl_divergence_nats=0.1,
            )
        )
        cands.append(
            EvidenceCandidate(
                market_address=m1,
                market_question="Q1",
                polarity="YES",
                note="4th YES fact exceeding cap",
                links=("https://example.org/m1/4th",),
                kl_divergence_nats=0.05,
            )
        )
        for m_addr, q in ((m2, "Q2"), (m3, "Q3"), (m4, "Q4")):
            for idx in range(3):
                cands.append(
                    EvidenceCandidate(
                        market_address=m_addr,
                        market_question=q,
                        polarity="NO",
                        note=f"Fact {idx} for {q}",
                        links=(f"https://example.org/{q}/{idx}",),
                        kl_divergence_nats=0.3 - idx * 0.02,
                    )
                )

        plan = build_submission_plan(cands, tier="standard", min_submissions=10, min_markets=3)
        self.assertTrue(plan.meets_competition_minimums)
        self.assertEqual(plan.total_submissions, 12)
        self.assertEqual(plan.distinct_markets, 4)
        m1_subs = [s for s in plan.submissions if s.market_address == m1]
        self.assertEqual(len(m1_subs), 3)
        self.assertTrue(all(s.directional_polarity == "YES" for s in m1_subs))
        self.assertTrue(any("Opposing polarity" in r for r in plan.rejections))
        self.assertTrue(any("Per-market cap" in r for r in plan.rejections))

    def test_execute_live_pipeline_enforces_limits_terms_and_entry_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfile = Path(tmp) / "cands.json"
            cfile.write_text(
                json.dumps([
                    {
                        "market_address": "0x94dbba7c9b44b788a701602ab6d919ff7b7fc38a",
                        "market_question": "Sample question",
                        "polarity": "YES",
                        "note": "Dated primary bulletin.",
                        "links": ["https://example.org/bulletin"],
                        "kl_divergence_nats": 0.42,
                    }
                ]),
                encoding="utf-8",
            )
            stub_ok = _StubEvidenceClient(submits_left=5, terms_accepted_at="2026-11-25T18:00:00Z")
            res_ok = run_evidence_cycle(cfile, execute_live=True, client=stub_ok)
            self.assertEqual(res_ok["live_writes_executed"], 1)
            self.assertEqual(len(stub_ok.submitted), 1)

            stub_no_tos = _StubEvidenceClient(terms_accepted_at=None)
            with self.assertRaises(RuntimeError):
                run_evidence_cycle(cfile, execute_live=True, client=stub_no_tos)
            self.assertEqual(len(stub_no_tos.submitted), 0)

            stub_zero = _StubEvidenceClient(submits_left=0)
            with self.assertRaises(RuntimeError):
                run_evidence_cycle(cfile, execute_live=True, client=stub_zero)

            stub_closed = _StubEvidenceClient(can_submit=False, remaining_on_market=0)
            res_closed = run_evidence_cycle(cfile, execute_live=True, client=stub_closed)
            self.assertEqual(res_closed["live_writes_executed"], 0)
            self.assertEqual(len(res_closed["skipped"]), 1)

    def test_dry_run_app_executes_zero_live_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mfile = Path(tmp) / "markets.json"
            mfile.write_text(json.dumps(_SAMPLE_MARKETS_FIXTURE), encoding="utf-8")
            result = run_dry_run_scan(markets_file=mfile, limit=5)
            self.assertEqual(result["mode"], "dry_run")
            self.assertEqual(result["live_writes_executed"], 0)
            self.assertEqual(result["markets_loaded"]["count"], 2)


if __name__ == "__main__":
    unittest.main()
