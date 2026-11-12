"""The Odds API v4 client + parsing of player-prop odds into per-line quotes.

To use a different feed, write a client exposing `events(sport_key)` and
`event_odds(sport_key, event_id, markets, bookmakers)` that return this same JSON shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import requests

from .names import norm_name


@dataclass
class SharpQuote:
    league: str
    stat: str
    player: str
    player_norm: str
    line: float
    event_id: str
    commence_ts: float | None
    home: str
    away: str
    books: dict = field(default_factory=dict)   # book -> {"over": dec, "under": dec, "ts": iso}
    main_books: set = field(default_factory=set)  # books for which this is the main (non-alternate) line


class OddsApiClient:
    def __init__(self, base: str, api_key: str, timeout: float = 12):
        self.base, self.key, self.timeout = base, api_key, timeout
        self.s = requests.Session()
        self.remaining: str | None = None
        self.used: str | None = None
        self._regions_fallback = False

    def _get(self, path: str, params: dict):
        r = self.s.get(self.base + path, params={"apiKey": self.key, **params}, timeout=self.timeout)
        self.remaining = r.headers.get("x-requests-remaining", self.remaining)
        self.used = r.headers.get("x-requests-used", self.used)
        return r

    def events(self, sport_key: str) -> list[dict]:
        r = self._get(f"/sports/{sport_key}/events", {"dateFormat": "iso"})
        r.raise_for_status()
        return r.json()

    def event_odds(self, sport_key: str, event_id: str, markets: list[str], bookmakers: list[str]) -> dict:
        params = {"markets": ",".join(markets), "oddsFormat": "decimal", "dateFormat": "iso"}
        path = f"/sports/{sport_key}/events/{event_id}/odds"
        if not self._regions_fallback:
            r = self._get(path, {**params, "bookmakers": ",".join(bookmakers)})
            if r.status_code != 422:
                r.raise_for_status()
                return r.json()
            self._regions_fallback = True  # a bookmaker key was rejected: fall back to regions
        r = self._get(path, {**params, "regions": "us,us2,eu"})
        r.raise_for_status()
        return r.json()


def _iso(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def parse_event_odds(payload: dict, league: str, market_to_stat: dict[str, str]) -> list[SharpQuote]:
    """Flatten one event's bookmaker odds into one SharpQuote per (player, stat, line)."""
    quotes: dict[tuple, SharpQuote] = {}
    for bk in payload.get("bookmakers") or []:
        book = bk.get("key")
        for mk in bk.get("markets") or []:
            key = str(mk.get("key") or "")
            is_alt = key.endswith("_alternate")
            stat = market_to_stat.get(key[: -len("_alternate")] if is_alt else key)
            if not stat:
                continue
            for o in mk.get("outcomes") or []:
                side = str(o.get("name") or "").lower()
                player, point, price = o.get("description"), o.get("point"), o.get("price")
                if side not in ("over", "under") or not player or point is None or not price:
                    continue
                pn = norm_name(player)
                k = (stat, pn, float(point))
                q = quotes.get(k)
                if q is None:
                    q = quotes[k] = SharpQuote(
                        league=league, stat=stat, player=player, player_norm=pn, line=float(point),
                        event_id=str(payload.get("id") or ""), commence_ts=_iso(payload.get("commence_time")),
                        home=str(payload.get("home_team") or ""), away=str(payload.get("away_team") or ""))
                q.books.setdefault(book, {"ts": mk.get("last_update")})[side] = float(price)
                if not is_alt:
                    q.main_books.add(book)
    return list(quotes.values())
