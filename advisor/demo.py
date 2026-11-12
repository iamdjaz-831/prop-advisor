"""Synthetic feeds for DEMO mode. Players are fictional; payloads mimic the real API shapes
so the same parsers, matching and signals run as in live mode."""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone

from .config import LEAGUES
from .injuries import parse_injuries
from .names import norm_name

# name, league, team, pos, {stat: mean}
PLAYERS = [
    ("Marcus Vell", "NBA", "Boston Celtics", "F", {"points": 27.4, "rebounds": 8.2, "assists": 5.1}),
    ("Deon Hartley", "NBA", "Boston Celtics", "G", {"points": 21.8, "assists": 6.3}),
    ("Tariq Osei", "NBA", "Boston Celtics", "C", {"points": 14.2, "rebounds": 9.6}),
    ("Jalen Brooksby", "NBA", "New York Knicks", "G", {"points": 25.9, "assists": 7.4}),
    ("Karl Antonov", "NBA", "New York Knicks", "C", {"points": 22.3, "rebounds": 11.7}),
    ("Nico Jovan", "NBA", "Denver Nuggets", "C", {"points": 28.8, "rebounds": 12.4, "assists": 9.8}),
    ("Jamal Mercer", "NBA", "Denver Nuggets", "G", {"points": 20.6, "assists": 6.0}),
    ("Luca Dante", "NBA", "Los Angeles Lakers", "G", {"points": 30.2, "rebounds": 8.0, "assists": 8.4}),
    ("Austin Reeve", "NBA", "Los Angeles Lakers", "G", {"points": 19.1, "assists": 5.2}),
    ("Josh Alder", "NFL", "Buffalo Bills", "QB", {"pass_yds": 248.0, "rush_yds": 34.0}),
    ("Khalil Shaw", "NFL", "Buffalo Bills", "WR", {"rec_yds": 61.0, "receptions": 4.8}),
    ("Jamie Cooke", "NFL", "Buffalo Bills", "RB", {"rush_yds": 72.0}),
    ("Tua Varga", "NFL", "Miami Dolphins", "QB", {"pass_yds": 236.0}),
    ("Tyrell Hill", "NFL", "Miami Dolphins", "WR", {"rec_yds": 78.0, "receptions": 6.1}),
    ("Caleb Wynn", "NFL", "Chicago Bears", "QB", {"pass_yds": 228.0, "rush_yds": 27.0}),
    ("DJ Morrow", "NFL", "Chicago Bears", "WR", {"rec_yds": 66.0, "receptions": 5.4}),
    ("Jordan Lowe", "NFL", "Green Bay Packers", "QB", {"pass_yds": 241.0}),
    ("Josh Jacoby", "NFL", "Green Bay Packers", "RB", {"rush_yds": 81.0}),
]
GAMES = [("NBA", "demo-nba-1", "Boston Celtics", "New York Knicks", 5), ("NBA", "demo-nba-2", "Los Angeles Lakers", "Denver Nuggets", 7),
         ("NFL", "demo-nfl-1", "Buffalo Bills", "Miami Dolphins", 20), ("NFL", "demo-nfl-2", "Chicago Bears", "Green Bay Packers", 23)]
CV = {"points": 0.27, "rebounds": 0.33, "assists": 0.36, "pass_yds": 0.24, "rush_yds": 0.42, "rec_yds": 0.45, "receptions": 0.38}
STEP = {"points": 2, "rebounds": 2, "assists": 2, "pass_yds": 25, "rush_yds": 10, "rec_yds": 10, "receptions": 1}
BOOKS = {"pinnacle": 0.035, "novig": 0.02, "draftkings": 0.07, "fanduel": 0.065, "betmgm": 0.075}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DemoWorld:
    def __init__(self, clock, seed: int = 11):
        self.clock, self.rng = clock, random.Random(seed)
        self.t0 = self.last = clock()
        self.mean = {(p[0], s): m for p in PLAYERS for s, m in p[4].items()}
        self.main = {k: round(m) - 0.5 for k, m in self.mean.items()}
        self.mkt = {}  # (player, stat, N) -> Kalshi market-maker state
        for (name, stat), line in self.main.items():
            for d in (-1, 0, 1):
                n = int(line + 0.5) + d * STEP[stat]
                if n > 0:
                    self.mkt[(name, stat, n)] = {"bias": self.rng.uniform(-0.05, 0.05), "half": self.rng.choice([0.01, 0.01, 0.02, 0.02, 0.03, 0.05]),
                                                 "size": self.rng.choice([4, 40, 120, 250, 600]), "vol": self.rng.choice([15, 300, 1500, 6000]), "fair": None}
        self.frozen: dict = {}
        self.inj = {"Tariq Osei": ("Questionable", "Ankle", self.t0 - 86400), "DJ Morrow": ("Questionable", "Hamstring", self.t0 - 90000)}
        self.remaining = "demo"
        self.step(force=True)

    def p_over(self, name: str, stat: str, line: float) -> float:
        mu = self.mean[(name, stat)]
        z = (line - mu) / (CV[stat] * mu)
        return min(max(0.5 * math.erfc(z / math.sqrt(2)), 0.02), 0.98)

    def step(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self.last < 5:
            return
        self.last = now
        for k in self.mean:
            self.mean[k] *= 1 + self.rng.gauss(0, 0.0015)
        if self.rng.random() < 0.12:  # steam: sharp books move, Kalshi makers lag for a few minutes
            k = self.rng.choice(list(self.mean))
            self.mean[k] *= 1 + self.rng.choice([-1, 1]) * self.rng.uniform(0.05, 0.09)
            self.frozen[k] = now + self.rng.uniform(150, 400)
            if abs(self.mean[k] - (self.main[k] + 0.5)) > 0.75 * max(1, STEP[k[1]] / 2):
                self.main[k] = round(self.mean[k]) - 0.5
        if now - self.t0 > 60 and "Deon Hartley" not in self.inj:  # scripted late scratches
            self.inj["Deon Hartley"] = ("Out", "Knee soreness, ruled out", now)
        if now - self.t0 > 150 and "Tyrell Hill" not in self.inj:
            self.inj["Tyrell Hill"] = ("Out", "Illness, downgraded to out", now)
        for (name, stat, n), m in self.mkt.items():
            true = self.p_over(name, stat, n - 0.5)
            if m["fair"] is None:
                m["fair"] = true
            elif self.frozen.get((name, stat), 0) < now:
                m["fair"] += 0.5 * (true - m["fair"]) + self.rng.gauss(0, 0.002)

    # ----- payloads in real API shapes -----
    def kalshi_markets(self, league: str, stat: str) -> list[dict]:
        self.step()
        label, out = LEAGUES[league]["stats"][stat]["label"], []
        team = {p[0]: p for p in PLAYERS}
        for (name, st, n), m in self.mkt.items():
            if st != stat or team[name][1] != league:
                continue
            mid = min(max(m["fair"] + m["bias"], 0.03), 0.97)
            bid, ask = max(round(mid - m["half"], 2), 0.01), min(round(mid + m["half"], 2), 0.99)
            out.append({
                "ticker": f"KX{league}{stat.upper().replace('_', '')}-DEMO-{''.join(w[0] for w in name.split()).upper()}{n}",
                "event_ticker": f"KX{league}{stat.upper()}-DEMO", "title": f"{name}: {n}+ {label}", "yes_sub_title": f"{name}: {n}+",
                "status": "active", "strike_type": "greater_or_equal", "floor_strike": n,
                "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}",
                "no_bid_dollars": f"{1 - ask:.4f}", "no_ask_dollars": f"{1 - bid:.4f}",
                "yes_ask_size_fp": f"{m['size']:.2f}", "yes_bid_size_fp": f"{m['size'] * 0.8:.2f}",
                "volume_24h_fp": f"{m['vol']:.2f}", "close_time": _iso(self.t0 + 3 * 86400),
            })
        return out

    def events(self, sport_key: str) -> list[dict]:
        lg = next(l for l, c in LEAGUES.items() if c["sport_key"] == sport_key)
        return [{"id": gid, "home_team": h, "away_team": a, "commence_time": _iso(self.t0 + hrs * 3600)}
                for l, gid, h, a, hrs in GAMES if l == lg]

    def event_odds(self, sport_key: str, event_id: str) -> dict:
        self.step()
        lg, _, home, away, hrs = next(g for g in GAMES if g[1] == event_id)
        bks = []
        for bi, (book, vig) in enumerate(BOOKS.items()):
            mk: dict = {}
            for name, plg, team, _pos, stats in PLAYERS:
                if team not in (home, away) or (book == "pinnacle" and sum(map(ord, name)) % 4 == 0):
                    continue
                if self.inj.get(name, ("",))[0] == "Out":
                    continue  # books pull props for players ruled out
                for stat in stats:
                    main = self.main[(name, stat)]
                    for d in (-1, 0, 1):
                        line = main + d * STEP[stat]
                        if line <= 0:
                            continue
                        p = min(max(self.p_over(name, stat, line) + (0.004 * ((bi * 7 + d) % 5 - 2) if book != "pinnacle" else 0), 0.02), 0.98)
                        key = LEAGUES[plg]["stats"][stat]["odds"] + ("" if d == 0 else "_alternate")
                        mk.setdefault(key, []).extend([
                            {"name": "Over", "description": name, "point": line, "price": round(1 / (p * (1 + vig)), 3)},
                            {"name": "Under", "description": name, "point": line, "price": round(1 / ((1 - p) * (1 + vig)), 3)}])
            bks.append({"key": book, "title": book, "markets": [{"key": k, "last_update": _iso(self.clock()), "outcomes": v} for k, v in mk.items()]})
        return {"id": event_id, "sport_key": sport_key, "commence_time": _iso(self.t0 + hrs * 3600),
                "home_team": home, "away_team": away, "bookmakers": bks}

    def injuries_payload(self, league: str) -> dict:
        self.step()
        teams: dict = {}
        for name, lg, team, pos, _ in PLAYERS:
            if lg == league and name in self.inj:
                status, comment, ts = self.inj[name]
                teams.setdefault(team, []).append({"athlete": {"displayName": name, "position": {"abbreviation": pos}},
                                                   "status": status, "date": _iso(ts), "shortComment": comment})
        return {"injuries": [{"displayName": t, "injuries": v} for t, v in teams.items()]}


class _Kalshi:
    def __init__(self, w): self.w = w
    def list_series(self, category="Sports"):
        return [{"ticker": f"KXDEMO{lg}{st.upper()}", "title": f"{cfg['aliases'][1]} player {sc['kw'][0]}"}
                for lg, cfg in LEAGUES.items() for st, sc in cfg["stats"].items() if st in CV]
    def markets_for_series(self, ticker):
        for lg, cfg in LEAGUES.items():
            for st in cfg["stats"]:
                if ticker == f"KXDEMO{lg}{st.upper()}":
                    return self.w.kalshi_markets(lg, st)
        return []


class _Odds:
    def __init__(self, w): self.w, self.remaining = w, "demo"
    def events(self, sport_key): return self.w.events(sport_key)
    def event_odds(self, sport_key, event_id, markets, bookmakers): return self.w.event_odds(sport_key, event_id)


class _Espn:
    def __init__(self, w): self.w = w
    def injuries(self, path):
        lg = next(l for l, c in LEAGUES.items() if c["espn"] == path)
        return parse_injuries(self.w.injuries_payload(lg))
    def rosters(self, path):
        lg = next(l for l, c in LEAGUES.items() if c["espn"] == path)
        return {norm_name(p[0]): {"team": p[2], "pos": p[3]} for p in PLAYERS if p[1] == lg}


class _Weather:
    def __init__(self, w): self.w = w
    def forecast(self, lat, lon):
        base = int(self.w.t0 // 3600) * 3600
        hours = [base + i * 3600 for i in range(72)]
        windy = abs(lat - 42.7738) < 0.01  # Buffalo
        return {"hourly": {"time": [datetime.fromtimestamp(h, timezone.utc).strftime("%Y-%m-%dT%H:%M") for h in hours],
                           "temperature_2m": [34.0 if windy else 48.0] * 72, "precipitation": [0.08 if windy else 0.0] * 72,
                           "wind_speed_10m": [22.0 if windy else 6.0] * 72, "wind_gusts_10m": [35.0 if windy else 11.0] * 72}}


def demo_clients(clock) -> dict:
    w = DemoWorld(clock)
    return {"kalshi": _Kalshi(w), "odds": _Odds(w), "espn": _Espn(w), "weather": _Weather(w), "world": w}
