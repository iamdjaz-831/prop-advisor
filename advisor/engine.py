"""Background poller: pulls every feed on its own timer and exposes a thread-safe snapshot."""
from __future__ import annotations

import threading
import time
import traceback

from .config import LEAGUES, Settings
from .injuries import EspnClient, InjuryMonitor
from .kalshi import KalshiClient, discover_series, parse_market
from .model import estimate, parse_gamelog, prev_season
from .names import norm_team
from .odds import OddsApiClient, parse_event_odds
from .signals import SteamDetector
from .weather import STADIUMS, WeatherClient, game_weather


class Engine:
    def __init__(self, settings: Settings, clients: dict | None = None, clock=time.time):
        self.cfg, self.now = settings, clock
        if clients is None:
            if settings.demo:
                from .demo import demo_clients
                clients = demo_clients(clock)
            else:
                clients = {"kalshi": KalshiClient(settings.kalshi_base),
                           "odds": OddsApiClient(settings.odds_base, settings.odds_api_key) if settings.odds_api_key else None,
                           "espn": EspnClient(), "weather": WeatherClient()}
        self.c = clients
        self.lock = threading.RLock()
        self.series: dict[tuple[str, str], list[str]] = {}
        self.kalshi: dict = {}            # ticker -> KalshiQuote
        self.sharp_by_event: dict = {}    # event_id -> [SharpQuote]
        self.events: dict = {}            # event_id -> {league, home, away, commence_ts}
        self.rosters: dict = {}           # league -> {player_norm: {team, pos}}
        self.weather: dict = {}           # event_id -> conditions
        self.inj = InjuryMonitor(settings.recent_injury_hours)
        self.steam = SteamDetector(settings.steam_window_s, settings.steam_min_dp)
        self.gamelogs: dict = {}          # (league, player) -> {"ts", "ok", "games"}
        self._memo: dict = {}             # (league, player, stat, line, log ts) -> estimate
        self.status: dict = {}
        self.diag: dict = {"series_counts": {}, "unparsed": 0}
        self._tasks = [  # name, fn, interval
            ["series", self._poll_series, 3600], ["rosters", self._poll_rosters, 12 * 3600],
            ["odds", self._poll_odds, settings.odds_poll_s], ["kalshi", self._poll_kalshi, settings.kalshi_poll_s],
            ["injuries", self._poll_injuries, settings.injury_poll_s], ["weather", self._poll_weather, settings.weather_poll_s],
            ["gamelogs", self._poll_gamelogs, 15],
        ]
        if self.c.get("odds") is None:  # Kalshi-only mode: no sharp feed, so no games/weather either
            self._tasks = [t for t in self._tasks if t[0] not in ("odds", "weather")]
        self._last: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- lifecycle ----------
    def start(self) -> "Engine":
        if not self._thread:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="prop-engine")
            self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(1.0)

    def run_once(self, force: bool = False) -> None:
        for name, fn, interval in self._tasks:
            now = self.now()
            if not force and now - self._last.get(name, -1e18) < interval:
                continue
            self._last[name] = now
            try:
                msg = fn() or ""
                self.status[name] = {"ok": True, "ts": self.now(), "msg": msg}
            except Exception as e:  # one bad feed must not stop the others
                self.status[name] = {"ok": False, "ts": self.now(), "msg": f"{type(e).__name__}: {e}"[:300],
                                     "trace": traceback.format_exc(limit=3)}

    # ---------- polls ----------
    def _poll_series(self) -> str:
        series = {(lg, st): list(sc["series"]) for lg, cfg in LEAGUES.items() for st, sc in cfg["stats"].items()}
        for item in self.cfg.extra_series:  # "NBA:points:TICKER"
            try:
                lg, st, tk = item.split(":")
                series[(lg.upper(), st)].append(tk)
            except (ValueError, KeyError):
                pass
        note = ""
        try:
            for k, tks in discover_series(self.c["kalshi"].list_series("Sports")).items():
                series[k].extend(tks)
        except Exception as e:
            note = f" (auto-discovery failed: {e})"
        with self.lock:
            self.series = {k: list(dict.fromkeys(v)) for k, v in series.items()}
        return f"{sum(len(v) for v in self.series.values())} series tickers{note}"

    def _poll_kalshi(self) -> str:
        now, quotes, counts, unparsed, errs = self.now(), {}, {}, 0, []
        for (lg, st), tickers in list(self.series.items()):
            others = [k for s2, sc in LEAGUES[lg]["stats"].items() if s2 != st for k in sc["kw"]]
            for tk in tickers:
                try:
                    raws = self.c["kalshi"].markets_for_series(tk)
                except Exception as e:
                    errs.append(f"{tk}: {e}"[:80])
                    counts[tk] = "error"
                    continue
                counts[tk] = len(raws)
                for raw in raws:
                    q = parse_market(raw, lg, st, now)
                    if q is None or any(f" {k} " in q.text_norm for k in others):
                        unparsed += 1  # no threshold found, or a combo of several stats
                        continue
                    quotes[q.ticker] = q
        with self.lock:
            self.kalshi = quotes
            self.diag.update(series_counts=counts, unparsed=unparsed)
            self.steam.record_kalshi(now, quotes.values())
        if errs and not quotes:
            raise RuntimeError("; ".join(errs[:3]))
        return f"{len(quotes)} prop markets from {sum(1 for v in counts.values() if v not in (0, 'error'))} series"

    def _poll_odds(self) -> str:
        now, n_events, by_event, events = self.now(), 0, {}, {}
        books = list(dict.fromkeys(self.cfg.sharp_books + self.cfg.consensus_books))
        errs = []
        for lg, cfg in LEAGUES.items():
            m2s = {sc["odds"]: st for st, sc in cfg["stats"].items()}
            markets = list(m2s) + ([f"{k}_alternate" for k in m2s] if self.cfg.include_alternates else [])
            try:
                evs = [e for e in self.c["odds"].events(cfg["sport_key"])
                       if (ts := _iso(e.get("commence_time"))) and now < ts < now + self.cfg.horizon_hours * 3600]
            except Exception as e:
                errs.append(f"{lg} events: {e}"[:120])
                continue
            evs.sort(key=lambda e: e["commence_time"])
            for e in evs[: self.cfg.max_events_per_poll]:
                try:
                    payload = self.c["odds"].event_odds(cfg["sport_key"], e["id"], markets, books)
                except Exception as ex:
                    errs.append(f"{lg} odds: {ex}"[:120])
                    continue
                by_event[e["id"]] = parse_event_odds(payload, lg, m2s)
                events[e["id"]] = {"league": lg, "home": e.get("home_team", ""), "away": e.get("away_team", ""),
                                   "commence_ts": _iso(e.get("commence_time"))}
                n_events += 1
        if errs and not by_event:
            raise RuntimeError("; ".join(errs[:3]))
        with self.lock:
            self.sharp_by_event, self.events = by_event, events
            allq = [q for qs in by_event.values() for q in qs]
            self.steam.record_sharp(now, allq, self.cfg.sharp_books, True)
        rem = getattr(self.c["odds"], "remaining", None)
        return (f"{len(allq)} player lines from {n_events} games" + (f", {rem} API credits left" if rem else "")
                + (f" | partial errors: {errs[0]}" if errs else ""))

    def _poll_injuries(self) -> str:
        n = 0
        for lg, cfg in LEAGUES.items():
            entries = self.c["espn"].injuries(cfg["espn"])
            with self.lock:
                self.inj.update(lg, entries, self.now())
            n += len(entries)
        return f"{n} players on reports"

    def _poll_rosters(self) -> str:
        for lg, cfg in LEAGUES.items():
            r = self.c["espn"].rosters(cfg["espn"])
            with self.lock:
                self.rosters[lg] = r
        return f"{sum(len(v) for v in self.rosters.values())} players"

    def _poll_weather(self) -> str:
        if not self.events:
            self._last["weather"] = -1e18  # retry as soon as the odds feed has games
            return "waiting for games"
        out, cache = {}, {}
        for eid, ev in list(self.events.items()):
            st = STADIUMS.get(norm_team(ev["home"])) if ev["league"] == "NFL" else None
            if not st or st[2] != "open" or not ev["commence_ts"]:
                continue
            if st[:2] not in cache:
                cache[st[:2]] = self.c["weather"].forecast(st[0], st[1])
            w = game_weather(cache[st[:2]], ev["commence_ts"])
            if w:
                out[eid] = {**w, "game": f"{ev['away']} @ {ev['home']}"}
        with self.lock:
            self.weather = out
        return f"{len(out)} outdoor NFL games, {sum(1 for w in out.values() if w['flag'])} flagged"

    def _poll_gamelogs(self) -> str:
        """Load game logs a few players at a time, only for players Kalshi lists props on."""
        now, want = self.now(), {}
        with self.lock:
            for kq in self.kalshi.values():
                r = self.rosters.get(kq.league, {}).get(kq.who)
                if r and r.get("id"):
                    want[(kq.league, kq.who)] = r["id"]
            due = [k for k in want if k not in self.gamelogs
                   or now - self.gamelogs[k]["ts"] > (6 * 3600 if self.gamelogs[k]["ok"] else 1800)]
        fails = 0
        for k in due[:25]:
            path = LEAGUES[k[0]]["espn"]
            try:
                games = parse_gamelog(self.c["espn"].gamelog(path, want[k]), True)
                games += parse_gamelog(self.c["espn"].gamelog(path, want[k], prev_season(k[0], now)), False)
                ok = True
            except Exception:
                games, ok, fails = [], False, fails + 1
            with self.lock:
                self.gamelogs[k] = {"ts": now, "ok": ok, "games": games}
            if not self.cfg.demo:
                time.sleep(0.1)
        done = sum(1 for k in want if k in self.gamelogs and self.gamelogs[k]["ok"])
        return f"game logs for {done} of {len(want)} players" + (f", {fails} failed this pass" if fails else "")

    def _model(self, kq) -> dict | None:
        log = self.gamelogs.get((kq.league, kq.who))
        if not log or not log["games"]:
            return None
        key = (kq.league, kq.who, kq.stat, kq.line, log["ts"])
        if key not in self._memo:
            if len(self._memo) > 50000:
                self._memo.clear()
            self._memo[key] = estimate(log["games"], kq.league, kq.stat, kq.line)
        return self._memo[key]

    # ---------- read side ----------
    def snapshot(self) -> dict:
        with self.lock:
            now = self.now()
            index: dict = {}  # (league, stat) -> {player_norm: {line: SharpQuote}}
            for qs in self.sharp_by_event.values():
                for q in qs:
                    index.setdefault((q.league, q.stat), {}).setdefault(q.player_norm, {})[q.line] = q
            matches, n_named = [], 0
            for kq in self.kalshi.values():
                players = index.get((kq.league, kq.stat), {})
                hit = max((p for p in players if f" {p} " in kq.text_norm), key=len, default=None)
                sq = near = None
                if hit:
                    n_named += 1
                    lines = players[hit]
                    sq = lines.get(kq.line)
                    if sq and kq.close_ts and sq.commence_ts and kq.close_ts < sq.commence_ts - 3600:
                        sq = None  # Kalshi market is for an earlier game
                    near = min(lines, key=lambda l: abs(l - kq.line))
                    player = next(iter(lines.values())).player
                else:
                    player = kq.title
                matches.append({"kq": kq, "sq": sq, "player": player, "nearest_line": near, "model": self._model(kq)})
            return {
                "now": now, "matches": matches, "alerts": self.steam.alerts(now, matches),
                "injuries": dict(self.inj.current), "changes": list(self.inj.changes),
                "rosters": self.rosters, "weather": dict(self.weather), "status": dict(self.status),
                "series": dict(self.series), "diag": dict(self.diag),
                "counts": {"kalshi": len(self.kalshi), "named": n_named,
                           "matched": sum(1 for m in matches if m["sq"]),
                           "sharp_lines": sum(len(v) for v in self.sharp_by_event.values())},
                "quota": getattr(self.c["odds"], "remaining", None), "demo": self.cfg.demo,
                "odds_enabled": self.c.get("odds") is not None,
            }


def _iso(s) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None
