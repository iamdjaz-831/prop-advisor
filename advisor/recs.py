"""Turn matched Kalshi/sharp pairs into ranked recommendation rows."""
from __future__ import annotations

import re
from datetime import datetime

from .config import LEAGUES
from .quant import fair_over, kalshi_fee, size_position
from .signals import context_flags


def build_rows(snap: dict, *, bankroll: float, kelly_frac: float, max_frac: float, method: str,
               sharp_books: list[str], allow_consensus: bool, fee_rate: float, fee_mult: float,
               max_spread: float, min_size: float, min_volume: float, lookback_s: float, now: float) -> list[dict]:
    rows = []
    for m in snap["matches"]:
        kq, sq = m["kq"], m["sq"]
        if sq is None:
            continue
        p_over, src = fair_over(sq.books, method, sharp_books, allow_consensus)
        if p_over is None:
            continue
        best = None
        for side, p, ask, size in (("YES", p_over, kq.yes_ask, kq.yes_ask_size),
                                   ("NO", 1 - p_over, kq.no_ask, kq.no_ask_size)):
            if ask is None:
                continue
            s = size_position(p, ask, bankroll, kelly_frac, max_frac, size, fee_rate, fee_mult)
            if best is None or s["edge_pct"] > best[1]["edge_pct"]:
                best = (side, s, p, ask, size)
        if best is None:
            continue
        side, s, p, ask, size = best
        spread = (kq.yes_ask - kq.yes_bid) if kq.yes_ask is not None and kq.yes_bid is not None else None
        why = []
        if spread is None:
            why.append("one-sided book")
        elif spread > max_spread + 1e-9:
            why.append(f"spread {spread * 100:.0f}c")
        if size is not None and size < min_size:
            why.append(f"only {size:.0f} at ask")
        if kq.volume_24h is not None and kq.volume_24h < min_volume:
            why.append(f"24h vol {kq.volume_24h:.0f}")
        flags, is_out = context_flags(kq, sq, sq.player_norm, snap, lookback_s, now)
        if size is None:
            flags.append("size at ask unknown")
        label = LEAGUES[kq.league]["stats"][kq.stat]["label"]
        book_q = sq.books.get(src, {})
        rows.append({
            "League": kq.league,
            "Player & Prop": f"{sq.player}: {'Over' if side == 'YES' else 'Under'} {sq.line:g} {label}",
            "Kalshi Contract": f"{side} {kq.ticker}",
            "Price (c)": round(ask * 100, 1),
            "Fair Value (c)": round(p * 100, 1),
            "Net Edge %": round(s["edge_pct"], 2),
            "EV/contract (c)": round(s["ev_per"] * 100, 2),
            "Fee/contract (c)": round(s["fee_per"] * 100, 2),
            "Contracts": s["contracts"],
            "Bet Size ($)": round(s["stake"], 2),
            "Spread (c)": None if spread is None else round(spread * 100, 1),
            "Size @ ask": size,
            "24h Vol": kq.volume_24h,
            "Fair Source": src,
            "Book Odds (O/U)": f"{book_q.get('over', 0):.2f} / {book_q.get('under', 0):.2f}" if book_q else "",
            "Flags": " | ".join(flags),
            "Game": f"{sq.away} @ {sq.home}",
            "Start": datetime.fromtimestamp(sq.commence_ts).strftime("%a %H:%M") if sq.commence_ts else "",
            "Filtered": ", ".join(why),
            "_out": is_out,
            "_capped": s["capped_by"],
        })
    rows.sort(key=lambda r: r["Net Edge %"], reverse=True)
    return rows


def kalshi_rows(snap: dict, *, max_spread: float, min_size: float, min_volume: float) -> list[dict]:
    """Kalshi-only view: live prop quotes with the liquidity filter, no fair value."""
    out_names = {k[1]: v for k, v in snap["injuries"].items() if v["bucket"] in ("OUT", "DOUBTFUL", "QUESTIONABLE")}
    rows = []
    for m in snap["matches"]:
        kq = m["kq"]
        spread = (kq.yes_ask - kq.yes_bid) if kq.yes_ask is not None and kq.yes_bid is not None else None
        why = []
        if spread is None:
            why.append("one-sided book")
        elif spread > max_spread + 1e-9:
            why.append(f"spread {spread * 100:.0f}c")
        if kq.yes_ask_size is not None and kq.yes_ask_size < min_size:
            why.append(f"only {kq.yes_ask_size:.0f} at ask")
        if kq.volume_24h is not None and kq.volume_24h < min_volume:
            why.append(f"24h vol {kq.volume_24h:.0f}")
        flags = [f"PLAYER {v['bucket']}" for n, v in out_names.items() if f" {n} " in kq.text_norm and v["league"] == kq.league]
        c = lambda x: None if x is None else round(x * 100, 1)
        # Market-implied chance: the midpoint of Kalshi's own bid and ask for YES.
        mid = (kq.yes_bid + kq.yes_ask) / 2 if spread is not None else None
        yes_side = mid is None or mid >= 0.5
        rows.append({"League": kq.league, "Market": kq.title, "Pick": "YES" if yes_side else "NO",
                     "Win % (market)": None if mid is None else round(100 * max(mid, 1 - mid), 1),
                     "Cost (c)": c(kq.yes_ask if yes_side else kq.no_ask),
                     "Spread (c)": c(spread),
                     "Size @ ask": kq.yes_ask_size if yes_side else kq.no_ask_size,
                     "24h Vol": kq.volume_24h, "Flags": " | ".join(flags),
                     "Stat": LEAGUES[kq.league]["stats"][kq.stat]["label"],
                     "YES bid (c)": c(kq.yes_bid), "YES ask (c)": c(kq.yes_ask), "NO ask (c)": c(kq.no_ask),
                     "Kalshi Contract": kq.ticker, "Filtered": ", ".join(why)})
    rows.sort(key=lambda r: -(r["Win % (market)"] or 0))
    return rows


def ladder_arbs(snap: dict, fee_rate: float, fee_mult: float) -> list[dict]:
    """Same player and stat, two thresholds: '25+' can never be likelier than '20+'.
    If YES on the lower line plus NO on the higher line costs under $1 after fees, one of
    them must pay $1 (both pay if he lands in between)."""
    groups: dict = {}
    for m in snap["matches"]:
        kq = m["kq"]
        who = re.sub(r"\d+(\.\d+)?\s*\+?", "", kq.title).strip().lower()
        groups.setdefault((kq.league, kq.stat, kq.event_ticker, who), []).append(kq)
    out = []
    for qs in groups.values():
        qs.sort(key=lambda q: q.line)
        for i, lo in enumerate(qs):
            for hi in qs[i + 1:]:
                if hi.line <= lo.line or lo.yes_ask is None or hi.no_ask is None:
                    continue
                n = 100  # fee is rounded per order, so quote it on a 100-lot
                cost = lo.yes_ask + hi.no_ask + (kalshi_fee(lo.yes_ask, n, fee_rate, fee_mult) + kalshi_fee(hi.no_ask, n, fee_rate, fee_mult)) / n
                if cost < 1:
                    size = min(x for x in (lo.yes_ask_size, hi.no_ask_size, 1e9) if x is not None)
                    out.append({"League": lo.league, "Buy YES": f"{lo.title} ({lo.ticker})", "YES ask (c)": round(lo.yes_ask * 100, 1),
                                "Buy NO": f"{hi.title} ({hi.ticker})", "NO ask (c)": round(hi.no_ask * 100, 1),
                                "Cost after fees (c)": round(cost * 100, 2), "Locked profit (c/pair)": round((1 - cost) * 100, 2),
                                "Pairs available": None if size >= 1e9 else size})
    out.sort(key=lambda r: -r["Locked profit (c/pair)"])
    return out
