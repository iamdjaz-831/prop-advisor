"""Stats-based win probability from a player's ESPN game log.

This is a simple second opinion, not a sharp price: it only knows what the player did in
recent games. It does not know about tonight's injuries, minutes limits, opponent or role.
"""
from __future__ import annotations

import math
from datetime import datetime

# our stat -> ESPN gamelog "names" key
STAT_KEYS = {
    "points": "points", "rebounds": "totalRebounds", "assists": "assists",
    "threes": "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
    "pass_yds": "passingYards", "rush_yds": "rushingYards", "rec_yds": "receivingYards",
    "receptions": "receptions", "pass_tds": "passingTouchdowns",
}
YARD_STATS = {"pass_yds", "rush_yds", "rec_yds"}
MIN_SD = {"pass_yds": 45.0, "rush_yds": 18.0, "rec_yds": 18.0}
PREV_SEASON_WEIGHT, RECENCY, MAX_PREV_GAMES, MIN_WEIGHT = 0.35, 0.95, 30, 4.0


def _num(s) -> float | None:
    try:
        return float(str(s).split("-")[0]) if str(s).strip() not in ("", "-", "--") else None
    except ValueError:
        return None


def parse_gamelog(payload: dict, current: bool) -> list[dict]:
    """Regular-season games, newest first: {"ts", "cur", "min", <stat>: value, ...}."""
    names = payload.get("names") or []
    idx = {stat: names.index(key) for stat, key in STAT_KEYS.items() if key in names}
    i_min = names.index("minutes") if "minutes" in names else None
    events = payload.get("events") or {}
    games = []
    for st in payload.get("seasonTypes") or []:
        if "regular" not in str(st.get("displayName", "")).lower():
            continue
        for cat in st.get("categories") or []:
            if cat.get("type") != "event":
                continue
            for ev in cat.get("events") or []:
                stats = ev.get("stats") or []
                g = {"cur": current, "ts": 0.0, "min": None}
                try:
                    g["ts"] = datetime.fromisoformat(str(events[ev["eventId"]]["gameDate"]).replace("Z", "+00:00")).timestamp()
                except (KeyError, ValueError, TypeError):
                    pass
                if i_min is not None and i_min < len(stats):
                    g["min"] = _num(stats[i_min])
                for stat, i in idx.items():
                    if i < len(stats) and (v := _num(stats[i])) is not None:
                        g[stat] = v
                games.append(g)
    games.sort(key=lambda g: -g["ts"])
    return games


def prev_season(league: str, now: float) -> int:
    """ESPN season id of the season before the current one."""
    d = datetime.fromtimestamp(now)
    if league == "NBA":  # NBA seasons are named for the year they end
        return d.year if d.month >= 9 else d.year - 1
    return d.year - 1 if d.month >= 8 else d.year - 2


def _nb_tail(k: int, mean: float, var: float) -> float:
    """P(X >= k) for a count stat: negative binomial when over-dispersed, else Poisson."""
    if k <= 0:
        return 1.0
    if mean <= 0:
        return 0.0
    cdf = 0.0
    if var > mean * 1.05:
        r = mean * mean / (var - mean)
        p = r / (r + mean)
        for x in range(k):
            cdf += math.exp(math.lgamma(x + r) - math.lgamma(r) - math.lgamma(x + 1) + r * math.log(p) + x * math.log(1 - p))
    else:
        for x in range(k):
            cdf += math.exp(-mean + x * math.log(mean) - math.lgamma(x + 1))
    return max(0.0, 1.0 - cdf)


def estimate(games: list[dict], league: str, stat: str, line: float) -> dict | None:
    """P(stat > line) from weighted recent games. None when there is too little history."""
    vals, w, i_cur, i_prev = [], [], 0, 0
    for g in games:  # newest first
        if stat not in g:
            continue
        if league == "NBA" and g["min"] is not None and g["min"] < 10:
            continue  # garbage time / left injured: not a normal game
        if g["cur"]:
            w.append(RECENCY ** i_cur)
            i_cur += 1
        else:
            if i_prev >= MAX_PREV_GAMES:
                continue
            w.append(PREV_SEASON_WEIGHT * 0.98 ** i_prev)
            i_prev += 1
        vals.append(g[stat])
    tot = sum(w)
    if tot < MIN_WEIGHT or len(vals) < 4:
        return None
    mean = sum(v * x for v, x in zip(vals, w)) / tot
    var = sum(x * (v - mean) ** 2 for v, x in zip(vals, w)) / tot
    if stat in YARD_STATS:
        sd = max(math.sqrt(var), 0.25 * abs(mean), MIN_SD[stat])
        p = 0.5 * math.erfc((line - mean) / (sd * math.sqrt(2)))
    else:
        p = _nb_tail(math.floor(line) + 1, mean, var)
    return {"p_over": min(max(p, 0.01), 0.99), "n": len(vals), "n_cur": i_cur, "avg": mean}
