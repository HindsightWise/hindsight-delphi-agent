"""CLI entrypoint for Hindsight-Delphi (DoraHacks #2479)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from src.app import check_live_access, run_dry_run_scan, run_evidence_cycle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hindsight-delphi",
        description="Hindsight-Delphi Evidence Mining Runtime for https://delphi.fyi/v1 (DoraHacks #2479).",
    )
    parser.add_argument("--markets-file", default=None, help="Path to competition_markets.json (defaults to live feed).")
    parser.add_argument("--candidates-file", default=None, help="Path to verified evidence candidates JSON.")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--tier", default="standard", choices=["restricted", "standard"])
    parser.add_argument("--dry-run", action="store_true", default=True, help="Zero-write mode (default).")
    parser.add_argument("--execute-live", action="store_true", help="Submit planned candidates to https://delphi.fyi/v1.")
    parser.add_argument("--check-live-auth", action="store_true", help="Verify DELPHI_API_ACCESS_KEY against /v1/limits.")
    parser.add_argument("--output", default="state/dry_run_bundle.json")
    args = parser.parse_args(argv)

    if args.candidates_file:
        result = run_evidence_cycle(
            args.candidates_file,
            execute_live=bool(args.execute_live),
            tier=args.tier,
        )
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(
            f"[hindsight-delphi] mode={result['mode']} planned={result['plan']['total_submissions']} "
            f"writes={result['live_writes_executed']} output={out_path}"
        )
        return 0

    if args.execute_live:
        print("[hindsight-delphi] --execute-live requires --candidates-file <json>", file=sys.stderr)
        return 2

    result = run_dry_run_scan(markets_file=args.markets_file, limit=args.limit, tier=args.tier)
    if args.check_live_auth:
        key = os.environ.get("DELPHI_API_ACCESS_KEY", "").strip()
        if not key:
            print("[hindsight-delphi] DELPHI_API_ACCESS_KEY is unset; create a Mainnet key at https://api-access.delphi.fyi", file=sys.stderr)
            return 2
        result["live_auth_check"] = check_live_access(key)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    loaded_count = result["markets_loaded"].get("count", 0)
    print(f"[hindsight-delphi] mode=dry_run markets_ranked={loaded_count} output={out_path}")
    return 0 if loaded_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
