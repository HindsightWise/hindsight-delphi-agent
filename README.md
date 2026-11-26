# Hindsight-Delphi

Deterministic, verbatim-grounded evidence-mining runtime for the [Gensyn Delphi Evidence API v1](https://delphi.fyi/v1) (`DoraHacks #2479`).

## Architecture

`Hindsight-Delphi` uses a strict vertical-slice architecture coordinated by an immutable event bus (`src/kernel/event_bus.py`) with zero cross-feature sibling imports (enforced via AST tests in `tests/test_arch_boundaries.py`):

- `src/kernel/event_bus.py`: Frozen pub/sub `ImmutableEventBus` with correlation IDs and immutable audit history.
- `src/features/competition_markets/`: Parses and ranks live open competition markets from `https://competition.delphi.fyi/markets.json`, filtering out markets past `evidenceCutoffAt` and prioritizing high-pot markets in the `20%–80%` uncertainty band.
- `src/features/evidence_api/`: Strict OpenAPI 3.1.0 client for `https://delphi.fyi/v1` (`GET /v1/limits`, `GET /v1/terms`, `POST /v1/terms/acceptance`, `GET /v1/markets/{market}/entry-state`, multipart `POST /v1/submissions`, `GET /v1/submissions/{id}`, `POST /v1/submissions/{id}/retry`) sending `Authorization: Bearer`, `X-Agent-Id`, `X-Agent-Harness`, `X-Agent-Model`, `Idempotency-Key`, and a Cloudflare-safe `User-Agent`.
- `src/features/submission_planner/`: Enforces Delphi competition guardrails prior to transmission:
  - **3-Item Total Cap:** Enforces at most `3` items combined across `note` (`<= 8,000` chars), `links[]` (`https://` only), and `files` (`<= 10 MB` total), matching OpenAPI 3.1.0 multipart field names (`marketAddress`, `note`, `links[]`, `files`).
  - **Anti-Cancellation Polarity Lock:** Locks each market to one consistent directional thesis (`YES` or `NO`) so opposing submissions never net out settlement value.
  - **Per-Market Cap & Target Tracking:** Caps each market at `3` submissions (`1` for restricted tier) while targeting `>= 10` judged submissions across `>= 3` distinct competition markets.
  - **Deterministic Idempotency & Verbatim Grounding:** Computes deterministic `SHA-256` `Idempotency-Key` headers for safe timeout retries, verifies verbatim quote substrings, and flags basic-fetch fragile domains (`x.com`, `reddit.com`, paywalls).

## Quickstart & Verification

Requires Python 3.11+ (zero external runtime dependencies; standard library only).

```bash
# Run vertical-slice boundary and API contract tests
python3 -m unittest discover -s tests -p 'test_*.py' -v

# Run a read-only dry-run market scan against live competition markets
python3 -m src.cli --dry-run --limit 12 --output state/dry_run_bundle.json
```

## Security & Credentials

- All API credentials stay strictly outside the repository in environment variables (`DELPHI_API_ACCESS_KEY` provisioned from `https://api-access.delphi.fyi`).
- Optional identity header overrides: `DELPHI_AGENT_ID`, `DELPHI_AGENT_HARNESS`, `DELPHI_AGENT_MODEL`.
- The runtime never auto-accepts Terms of Service without explicit operator action and defaults to zero-write dry-run mode.

## License

MIT — see [LICENSE](LICENSE).
