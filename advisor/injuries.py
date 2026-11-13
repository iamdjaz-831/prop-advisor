"""ESPN public injury/roster endpoints and a monitor that detects status changes."""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime

import requests

from .names import norm_name, norm_team


def bucket(status: str) -> str:
    s = (status or "").lower()
    if "doubt" in s:
        return "DOUBTFUL"
    if "question" in s or "day-to-day" in s or "day to day" in s or "game-time" in s:
        return "QUESTIONABLE"
    if "prob" in s:
        return "PROBABLE"
    if "out" in s or "reserve" in s or "suspen" in s or s in ("ir", "pup"):
        return "OUT"
    if "active" in s:
        return "ACTIVE"
    return (status or "UNKNOWN").upper()


def parse_injuries(payload: dict) -> list[dict]:
    out = []
    for team in payload.get("injuries") or []:
        tname = team.get("displayName") or (team.get("team") or {}).get("displayName") or ""
        for it in team.get("injuries") or []:
            ath = it.get("athlete") or {}
            name = ath.get("displayName") or ath.get("fullName")
            if not name:
                continue
            status = it.get("status")
            if not isinstance(status, str):
                status = (it.get("type") or {}).get("description") or ""
            out.append({
                "name": name, "team": tname, "pos": (ath.get("position") or {}).get("abbreviation") or "",
                "status": status, "date": it.get("date"), "comment": it.get("shortComment") or "",
            })
    return out


class EspnClient:
    BASE = "https://site.api.espn.com/apis/site/v2/sports"

    def __init__(self, timeout: float = 10):
        self.s = requests.Session()
        self.timeout = timeout

    def _get(self, path: str) -> dict:
        r = self.s.get(f"{self.BASE}/{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def injuries(self, league_path: str) -> list[dict]:
        return parse_injuries(self._get(f"{league_path}/injuries"))

    def rosters(self, league_path: str) -> dict[str, dict]:
        """normalised player name -> {"team", "pos"}; one request per team, so cache it."""
        out: dict[str, dict] = {}
        data = self._get(f"{league_path}/teams")
        teams = [t["team"] for t in data["sports"][0]["leagues"][0]["teams"]]
        for t in teams:
            try:
                ros = self._get(f"{league_path}/teams/{t['id']}/roster")
            except requests.RequestException:
                continue
            groups = ros.get("athletes") or []
            athletes = []
            for g in groups:  # NFL groups by unit ({"items": [...]}), NBA is a flat list
                athletes.extend(g["items"] if isinstance(g, dict) and "items" in g else [g])
            for a in athletes:
                nm = a.get("displayName") or a.get("fullName")
                if nm:
                    out[norm_name(nm)] = {"team": t.get("displayName", ""), "id": str(a.get("id") or ""),
                                          "pos": (a.get("position") or {}).get("abbreviation") or ""}
            time.sleep(0.05)
        return out


    def gamelog(self, league_path: str, athlete_id: str, season: int | None = None) -> dict:
        r = self.s.get(f"https://site.web.api.espn.com/apis/common/v3/sports/{league_path}/athletes/{athlete_id}/gamelog",
                       params={"season": season} if season else None, timeout=self.timeout)
        r.raise_for_status()
        return r.json()


class InjuryMonitor:
    """Keeps the latest report per league and a log of status changes."""

    def __init__(self, recent_hours: float = 3):
        self.current: dict[tuple[str, str], dict] = {}
        self.changes: deque = deque(maxlen=500)
        self.recent_hours = recent_hours
        self._seeded: set[str] = set()

    def update(self, league: str, entries: list[dict], now: float) -> None:
        seen = set()
        first = league not in self._seeded
        for e in entries:
            key = (league, norm_name(e["name"]))
            seen.add(key)
            old = self.current.get(key)
            new_b = bucket(e["status"])
            rec = {**e, "league": league, "bucket": new_b, "team_norm": norm_team(e["team"])}
            if old is None:
                if not first:
                    self._log(now, rec, "not listed")
                else:  # first load: surface entries ESPN stamped within the last few hours
                    ts = _iso(e.get("date"))
                    if ts and now - ts < self.recent_hours * 3600:
                        self._log(ts, rec, "report update")
            elif old["bucket"] != new_b:
                self._log(now, rec, old["bucket"])
            self.current[key] = rec
        for key in [k for k in self.current if k[0] == league and k not in seen]:
            old = self.current.pop(key)
            self._log(now, {**old, "bucket": "ACTIVE", "status": "Off report", "comment": ""}, old["bucket"])
        self._seeded.add(league)

    def _log(self, ts: float, rec: dict, old: str) -> None:
        self.changes.appendleft({"ts": ts, "league": rec["league"], "name": rec["name"], "team": rec["team"],
                                 "team_norm": rec["team_norm"], "pos": rec["pos"], "old": old,
                                 "new": rec["bucket"], "comment": rec["comment"]})

    def status_of(self, league: str, player_norm: str) -> dict | None:
        return self.current.get((league, player_norm))


def _iso(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None
