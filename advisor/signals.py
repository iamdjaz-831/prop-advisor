"""Steam detection (sharp move vs stale Kalshi quote) and bottom-up context flags."""
from __future__ import annotations

from collections import defaultdict, deque

from .config import LEAGUES
from .names import norm_name, norm_team
from .quant import fair_over

PASS_STATS = {"pass_yds", "pass_tds", "rec_yds", "receptions"}


class SteamDetector:
    def __init__(self, window_s: float = 600, min_dp: float = 0.03, stale_tol: float = 0.011, min_span_s: float = 45):
        self.window_s, self.min_dp, self.stale_tol, self.min_span_s = window_s, min_dp, stale_tol, min_span_s
        self.fair: dict = defaultdict(deque)   # (league, stat, player, line) -> (ts, p_over)
        self.main: dict = defaultdict(deque)   # (league, stat, player) -> (ts, main line)
        self.kq: dict = defaultdict(deque)     # ticker -> (ts, yes_bid, yes_ask)

    def _push(self, dq: deque, item: tuple) -> None:
        dq.append(item)
        cutoff = item[0] - 2 * self.window_s
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def record_sharp(self, now: float, quotes, sharp_books: list[str], allow_consensus: bool) -> None:
        mains: dict = {}
        for q in quotes:
            p, _ = fair_over(q.books, "multiplicative", sharp_books, allow_consensus)
            if p is not None:
                self._push(self.fair[(q.league, q.stat, q.player_norm, q.line)], (now, p))
            for rank, b in enumerate(sharp_books + sorted(q.main_books)):
                if b in q.main_books:  # main line of the highest-priority book
                    k = (q.league, q.stat, q.player_norm)
                    if k not in mains or rank < mains[k][0]:
                        mains[k] = (rank, q.line)
                    break
        for k, (_, line) in mains.items():
            self._push(self.main[k], (now, line))

    def record_kalshi(self, now: float, quotes) -> None:
        for q in quotes:
            self._push(self.kq[q.ticker], (now, q.yes_bid, q.yes_ask))

    def _window(self, dq: deque, now: float):
        pts = [x for x in dq if x[0] >= now - self.window_s]
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < self.min_span_s:
            return None
        return pts

    def _kalshi_stale(self, ticker: str, t_ref: float, up: bool):
        """Has the Kalshi price on the side you'd buy stayed put since the move began?"""
        dq = self.kq.get(ticker)
        if not dq:
            return None
        then = next((x for x in dq if x[0] >= t_ref), dq[0])
        cur = dq[-1]
        if up:  # you would buy YES: is the YES ask still where it was?
            if then[2] is None or cur[2] is None:
                return None
            return cur[2] <= then[2] + self.stale_tol, then[2], cur[2]
        if then[1] is None or cur[1] is None:  # you would buy NO = hit the YES bid
            return None
        return cur[1] >= then[1] - self.stale_tol, 1 - then[1], 1 - cur[1]

    def alerts(self, now: float, matches) -> list[dict]:
        out = []
        for m in matches:
            kq, sq = m["kq"], m["sq"]
            if sq is None:
                continue
            base = {"league": kq.league, "player": m["player"], "stat": kq.stat, "ticker": kq.ticker,
                    "market": f"{m['player']} {kq.label} {LEAGUES[kq.league]['stats'][kq.stat]['label']}"}
            # 1) juice move: fair probability at this exact line shifted
            pts = self._window(self.fair.get((kq.league, kq.stat, sq.player_norm, sq.line), ()), now)
            if pts:
                # largest move ending now, measured from the extreme inside the window
                lo, hi = min(pts, key=lambda x: x[1]), max(pts, key=lambda x: x[1])
                cur = pts[-1]
                ref = lo if cur[1] - lo[1] >= hi[1] - cur[1] else hi
                dp = cur[1] - ref[1]
                if abs(dp) >= self.min_dp:
                    st = self._kalshi_stale(kq.ticker, ref[0], dp > 0)
                    if st:
                        out.append({**base, "kind": "juice", "side": "YES" if dp > 0 else "NO",
                                    "detail": f"Over {sq.line:g} fair {ref[1]:.1%} -> {cur[1]:.1%}",
                                    "move_pts": 100 * dp, "mins": (cur[0] - ref[0]) / 60,
                                    "stale": st[0], "k_then": st[1], "k_now": st[2]})
            # 2) line move: the sharp main line itself was moved
            pts = self._window(self.main.get((kq.league, kq.stat, sq.player_norm), ()), now)
            if pts and pts[-1][1] != pts[0][1]:
                first_new = next(x for x in pts if x[1] != pts[0][1])
                up = pts[-1][1] > pts[0][1]
                st = self._kalshi_stale(kq.ticker, first_new[0] - 1, up)
                if st:
                    out.append({**base, "kind": "line", "side": "YES" if up else "NO",
                                "detail": f"main line {pts[0][1]:g} -> {pts[-1][1]:g}",
                                "move_pts": None, "mins": (now - first_new[0]) / 60,
                                "stale": st[0], "k_then": st[1], "k_now": st[2]})
        out.sort(key=lambda a: (not a["stale"], -(abs(a["move_pts"] or 99))))
        return out


def context_flags(kq, sq, player_norm: str, snap: dict, lookback_s: float, now: float) -> tuple[list[str], bool]:
    """Bottom-up notes for one prop. Returns (flags, player_is_out)."""
    flags, is_out = [], False
    league, stat = kq.league, kq.stat
    own = snap["injuries"].get((league, player_norm))
    if own and own["bucket"] in ("OUT", "DOUBTFUL", "QUESTIONABLE"):
        flags.append(f"PLAYER {own['bucket']}")
        is_out = own["bucket"] in ("OUT", "DOUBTFUL")
    ros = snap["rosters"].get(league, {}).get(player_norm)
    team = norm_team(ros["team"]) if ros else None
    game_teams = {norm_team(sq.home), norm_team(sq.away)} if sq else set()
    for ch in snap["changes"]:
        if ch["league"] != league or now - ch["ts"] > lookback_s or ch["new"] not in ("OUT", "DOUBTFUL"):
            continue
        if norm_name(ch["name"]) == player_norm:
            continue
        same_team = team is not None and ch["team_norm"] == team
        if not same_team and not (team is None and ch["team_norm"] in game_teams):
            continue
        hint = _usage_hint(league, stat, ch["pos"])
        if hint:
            who = "teammate" if same_team else "player in this game"
            flags.append(f"{who} {ch['name']} ({ch['pos'] or '?'}) newly {ch['new']}: {hint}")
    if league == "NFL" and sq is not None:
        w = snap["weather"].get(sq.event_id)
        if w and w.get("flag") and stat in PASS_STATS:
            flags.append(f"weather {w['flag']}: lean UNDER passing")
    return flags, is_out


def _usage_hint(league: str, stat: str, out_pos: str) -> str:
    if league == "NBA":
        return "usage up, lean OVER"
    p = (out_pos or "").upper()
    if p == "QB":
        return "backup QB, lean UNDER passing game" if stat in PASS_STATS else "run-heavier script, lean OVER"
    if p in ("WR", "TE") and stat in ("rec_yds", "receptions"):
        return "target share up, lean OVER"
    if p == "RB" and stat == "rush_yds":
        return "carry share up, lean OVER"
    return ""
