"""Kalshi public REST client (market data needs no API key) and market parsing."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from .config import LEAGUES, SERIES_EXCLUDE
from .names import norm_name


@dataclass
class KalshiQuote:
    ticker: str
    event_ticker: str
    league: str
    stat: str
    title: str
    text_norm: str        # normalised title text, space padded, for player matching
    line: float           # sportsbook-equivalent line: YES == Over `line`
    label: str            # e.g. "25+"
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    yes_ask_size: float | None
    no_ask_size: float | None
    volume_24h: float | None
    close_ts: float | None
    ts: float
    who: str = ""         # normalised player name from the title ("Name: 25+ points")


class KalshiClient:
    def __init__(self, base: str, timeout: float = 10):
        self.base, self.timeout = base, timeout
        self.s = requests.Session()
        self.s.headers["Accept"] = "application/json"

    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(3):
            r = self.s.get(self.base + path, params=params, timeout=self.timeout)
            if r.status_code == 429:  # rate limited: back off and retry
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def list_series(self, category: str = "Sports") -> list[dict]:
        return self._get("/series", {"category": category}).get("series") or []

    def markets_for_series(self, series_ticker: str) -> list[dict]:
        out, cursor = [], None
        for _ in range(20):
            params = {"series_ticker": series_ticker, "status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params)
            out.extend(data.get("markets") or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        return out


def discover_series(series: list[dict]) -> dict[tuple[str, str], list[str]]:
    """Map (league, stat) -> series tickers by reading series titles."""
    found: dict[tuple[str, str], list[str]] = {}
    for s in series:
        ticker = str(s.get("ticker") or "")
        title = " " + norm_name(str(s.get("title") or "")) + " "
        if any(f" {w} " in title or w in title.split() for w in SERIES_EXCLUDE):
            continue
        for lg, cfg in LEAGUES.items():
            in_league = ticker.upper().startswith(cfg["ticker_prefix"]) or any(f" {a} " in title for a in cfg["aliases"])
            if not in_league:
                continue
            for stat, sc in cfg["stats"].items():
                if all(k in title for k in sc["kw"]):
                    found.setdefault((lg, stat), []).append(ticker)
    return found


def _price(raw: dict, key: str) -> float | None:
    v = raw.get(key + "_dollars")
    try:
        if v not in (None, ""):
            p = float(v)
        elif raw.get(key) is not None:
            p = float(raw[key]) / 100  # legacy integer cents
        else:
            return None
    except (TypeError, ValueError):
        return None
    return p if 0 < p < 1 else None  # 0 / 1.00 means an empty side of the book


def _num(raw: dict, *keys: str) -> float | None:
    for k in keys:
        v = raw.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _iso(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


_PLUS = re.compile(r"(\d+(?:\.\d+)?)\s*\+")
_ATLEAST = re.compile(r"(?:at least|or more than|or more)\D{0,3}(\d+)|(\d+)\s+or more", re.I)
_OVER = re.compile(r"(?:over|more than|above)\s+(\d+(?:\.\d+)?)", re.I)


def strike_to_line(raw: dict) -> tuple[float, str] | None:
    """Convert a Kalshi threshold into the sportsbook line where YES == Over.
    '25+ points' -> Over 24.5.  'more than 24.5' -> Over 24.5.  'more than 24' -> Over 24.5."""
    text = " ".join(str(raw.get(k) or "") for k in ("yes_sub_title", "title", "subtitle"))
    m = _PLUS.search(text)
    if m:
        n = float(m.group(1))
        return (n - 0.5 if n.is_integer() else n), f"{m.group(1)}+"
    m = _ATLEAST.search(text)
    if m:
        n = float(m.group(1) or m.group(2))
        return n - 0.5, f"{int(n)}+"
    st = str(raw.get("strike_type") or "").lower()
    fs = _num(raw, "floor_strike")
    if fs is not None and st in ("greater_or_equal", "greater"):
        if not fs.is_integer():
            return fs, f"over {fs:g}"
        return (fs - 0.5, f"{int(fs)}+") if st == "greater_or_equal" else (fs + 0.5, f"{int(fs) + 1}+")
    m = _OVER.search(text)
    if m:
        n = float(m.group(1))
        return (n + 0.5 if n.is_integer() else n), f"over {m.group(1)}"
    return None


def _who(raw: dict) -> str:
    for k in ("yes_sub_title", "title"):
        t = str(raw.get(k) or "")
        if ":" in t:
            return norm_name(t.split(":")[0])
    return ""


def parse_market(raw: dict, league: str, stat: str, now: float) -> KalshiQuote | None:
    sl = strike_to_line(raw)
    if not sl or not raw.get("ticker"):
        return None
    yes_bid, yes_ask = _price(raw, "yes_bid"), _price(raw, "yes_ask")
    no_bid, no_ask = _price(raw, "no_bid"), _price(raw, "no_ask")
    if no_ask is None and yes_bid is not None:
        no_ask = round(1 - yes_bid, 4)  # buying NO lifts the YES bid
    if no_bid is None and yes_ask is not None:
        no_bid = round(1 - yes_ask, 4)
    text = " ".join(str(raw.get(k) or "") for k in ("title", "yes_sub_title", "subtitle"))
    return KalshiQuote(
        ticker=raw["ticker"], event_ticker=str(raw.get("event_ticker") or ""), league=league, stat=stat,
        title=str(raw.get("title") or raw.get("yes_sub_title") or raw["ticker"]),
        text_norm=" " + norm_name(text) + " ", line=sl[0], label=sl[1],
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
        yes_ask_size=_num(raw, "yes_ask_size_fp", "yes_ask_size"),
        no_ask_size=_num(raw, "yes_bid_size_fp", "yes_bid_size"),
        volume_24h=_num(raw, "volume_24h_fp", "volume_24h"),
        close_ts=_iso(raw.get("expected_expiration_time")) or _iso(raw.get("close_time")),
        ts=now, who=_who(raw),
    )
