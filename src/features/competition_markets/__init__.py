"""Competition markets discovery and ranking brick."""

from ._loader import (
    COMPETITION_MARKETS_URL,
    CompetitionMarket,
    fetch_live_competition_markets,
    load_competition_markets_file,
    parse_and_rank_markets,
    register,
)

__all__ = [
    "COMPETITION_MARKETS_URL",
    "CompetitionMarket",
    "fetch_live_competition_markets",
    "load_competition_markets_file",
    "parse_and_rank_markets",
    "register",
]
