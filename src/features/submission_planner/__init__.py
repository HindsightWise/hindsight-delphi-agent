"""Competition submission planner enforcing polarity lock, caps, and idempotency."""

from ._planner import (
    BASIC_FETCH_FRAGILE_DOMAINS,
    COMPETITION_MIN_MARKETS,
    COMPETITION_MIN_SUBMISSIONS,
    PER_MARKET_CAP_BY_TIER,
    EvidenceCandidate,
    PlannedSubmission,
    SubmissionPlan,
    build_submission_plan,
    compute_idempotency_key,
    register,
    verify_verbatim_quote,
)

__all__ = [
    "BASIC_FETCH_FRAGILE_DOMAINS",
    "COMPETITION_MIN_MARKETS",
    "COMPETITION_MIN_SUBMISSIONS",
    "PER_MARKET_CAP_BY_TIER",
    "EvidenceCandidate",
    "PlannedSubmission",
    "SubmissionPlan",
    "build_submission_plan",
    "compute_idempotency_key",
    "register",
    "verify_verbatim_quote",
]
