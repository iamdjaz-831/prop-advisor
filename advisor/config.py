"""Settings and static league/stat definitions. Everything here is overridable via .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Tiny .env loader (no dependency). Existing environment variables win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# stat -> odds: The Odds API market key | label: wording | series: Kalshi series tickers to try
#         kw: words that must appear in a Kalshi series title for auto-discovery
LEAGUES: dict[str, dict] = {
    "NBA": {
        "sport_key": "basketball_nba",
        "espn": "basketball/nba",
        "aliases": ["nba", "pro basketball"],
        "ticker_prefix": "KXNBA",
        "stats": {
            "points": {"odds": "player_points", "label": "points", "series": ["KXNBAPTS"], "kw": ["points"]},
            "rebounds": {"odds": "player_rebounds", "label": "rebounds", "series": ["KXNBAREB"], "kw": ["rebounds"]},
            "assists": {"odds": "player_assists", "label": "assists", "series": ["KXNBAAST"], "kw": ["assists"]},
            "threes": {"odds": "player_threes", "label": "threes", "series": ["KXNBA3PT"], "kw": ["three"]},
        },
    },
    "NFL": {
        "sport_key": "americanfootball_nfl",
        "espn": "football/nfl",
        "aliases": ["nfl", "pro football"],
        "ticker_prefix": "KXNFL",
        "stats": {
            "pass_yds": {"odds": "player_pass_yds", "label": "passing yards", "series": ["KXNFLPASSYDS"], "kw": ["passing yards"]},
            "rush_yds": {"odds": "player_rush_yds", "label": "rushing yards", "series": ["KXNFLRUSHYDS"], "kw": ["rushing yards"]},
            "rec_yds": {"odds": "player_reception_yds", "label": "receiving yards", "series": ["KXNFLRECYDS"], "kw": ["receiving yards"]},
            "receptions": {"odds": "player_receptions", "label": "receptions", "series": ["KXNFLREC"], "kw": ["receptions"]},
            "pass_tds": {"odds": "player_pass_tds", "label": "passing TDs", "series": ["KXNFLPASSTDS"], "kw": ["passing touchdowns"]},
        },
    },
}

# Words that disqualify a Kalshi series from auto-discovery (team/game/season markets).
SERIES_EXCLUDE = [
    "team", "total", "spread", "winner", "combo", "parlay", "mvp", "season", "champion",
    "leader", "first", "quarter", "half", "draft", "award", "wins", "game:",
]


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


@dataclass
class Settings:
    odds_api_key: str = ""
    demo: bool = True
    kalshi_base: str = "https://api.elections.kalshi.com/trade-api/v2"
    odds_base: str = "https://api.the-odds-api.com/v4"
    # Priority order: the first book quoting both sides of the exact line is the fair value.
    sharp_books: list[str] = field(default_factory=lambda: ["pinnacle", "novig", "prophetx", "betonlineag", "lowvig"])
    consensus_books: list[str] = field(default_factory=lambda: ["draftkings", "fanduel", "betmgm", "williamhill_us"])
    include_alternates: bool = True
    extra_series: list[str] = field(default_factory=list)  # "NBA:points:TICKER"
    kalshi_poll_s: float = 20
    odds_poll_s: float = 60
    injury_poll_s: float = 30
    weather_poll_s: float = 900
    horizon_hours: float = 36
    max_events_per_poll: int = 12
    fee_rate: float = 0.07
    fee_multiplier: float = 1.0
    steam_window_s: float = 600
    steam_min_dp: float = 0.03
    recent_injury_hours: float = 3

    @classmethod
    def from_env(cls, root: Path | None = None) -> "Settings":
        if root:
            load_dotenv(root / ".env")
        key = os.environ.get("ODDS_API_KEY", "").strip()
        demo_env = os.environ.get("DEMO_MODE", "").strip().lower()
        # No key -> Kalshi-only live mode. Simulated demo data only when DEMO_MODE=1.
        demo = demo_env in ("1", "true", "yes")
        s = cls(odds_api_key=key, demo=demo)
        s.kalshi_base = os.environ.get("KALSHI_BASE_URL", s.kalshi_base).rstrip("/")
        s.odds_base = os.environ.get("ODDS_API_BASE_URL", s.odds_base).rstrip("/")
        s.sharp_books = _list("SHARP_BOOKS", ",".join(s.sharp_books))
        s.consensus_books = _list("CONSENSUS_BOOKS", ",".join(s.consensus_books))
        s.include_alternates = os.environ.get("INCLUDE_ALTERNATES", "1").lower() in ("1", "true", "yes")
        s.extra_series = _list("KALSHI_SERIES", "")
        s.kalshi_poll_s = _f("KALSHI_POLL_SECONDS", s.kalshi_poll_s)
        s.odds_poll_s = _f("ODDS_POLL_SECONDS", s.odds_poll_s)
        s.injury_poll_s = _f("INJURY_POLL_SECONDS", s.injury_poll_s)
        s.horizon_hours = _f("HORIZON_HOURS", s.horizon_hours)
        s.max_events_per_poll = int(_f("MAX_EVENTS_PER_POLL", s.max_events_per_poll))
        s.fee_rate = _f("KALSHI_FEE_RATE", s.fee_rate)
        s.fee_multiplier = _f("KALSHI_FEE_MULTIPLIER", s.fee_multiplier)
        s.steam_window_s = _f("STEAM_WINDOW_SECONDS", s.steam_window_s)
        s.steam_min_dp = _f("STEAM_MIN_PROB_MOVE", s.steam_min_dp)
        return s
