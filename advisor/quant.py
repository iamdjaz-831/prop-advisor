"""Pricing maths: devig, Kalshi fee, expected value, fractional Kelly."""
from __future__ import annotations

import math
from statistics import median


def devig_two_way(over: float, under: float, method: str = "multiplicative") -> float | None:
    """Vig-free P(over) from decimal odds on both sides of one line."""
    if not over or not under or over <= 1 or under <= 1:
        return None
    io, iu = 1 / over, 1 / under
    if method == "additive":
        return min(max(io - (io + iu - 1) / 2, 0.001), 0.999)
    if method == "power" and io + iu > 1:
        lo, hi = 1.0, 50.0  # find k with io^k + iu^k = 1
        for _ in range(60):
            k = (lo + hi) / 2
            if io**k + iu**k > 1:
                lo = k
            else:
                hi = k
        return io**k
    return io / (io + iu)


def fair_over(books: dict, method: str, sharp_books: list[str], allow_consensus: bool):
    """Return (P(over), source). First sharp book in priority order wins; else median of
    every two-sided book if consensus is allowed and at least two books quote it."""
    for b in sharp_books:
        q = books.get(b)
        if q:
            p = devig_two_way(q.get("over"), q.get("under"), method)
            if p is not None:
                return p, b
    if allow_consensus:
        ps = [p for q in books.values() if (p := devig_two_way(q.get("over"), q.get("under"), method)) is not None]
        if len(ps) >= 2:
            return median(ps), f"consensus({len(ps)})"
    return None, None


def kalshi_fee(price: float, contracts: int, rate: float = 0.07, multiplier: float = 1.0) -> float:
    """Taker fee in dollars for a whole order: round up(M x rate x C x P x (1-P)) to the cent."""
    if contracts <= 0:
        return 0.0
    raw_cents = rate * multiplier * contracts * price * (1 - price) * 100
    return math.ceil(raw_cents - 1e-9) / 100


def size_position(p: float, ask: float, bankroll: float, kelly_frac: float = 0.25,
                  max_frac: float = 0.02, avail: float | None = None,
                  rate: float = 0.07, multiplier: float = 1.0) -> dict:
    """Edge and stake for buying one side at `ask` when its true win probability is `p`.

    Net edge % = EV per contract / all-in cost per contract (price + fee).
    Kelly for a binary paying $1: f* = (p - cost) / (1 - cost).
    """
    fee_u = rate * multiplier * ask * (1 - ask)  # unrounded per-contract fee
    cost = ask + fee_u
    ev = p - cost
    out = {"contracts": 0, "stake": 0.0, "fee_per": fee_u, "cost_per": cost, "ev_per": ev,
           "edge_pct": 100 * ev / cost, "kelly_full": 0.0, "capped_by": ""}
    if ev <= 0 or cost >= 1:
        return out
    f = (p - cost) / (1 - cost)
    out["kelly_full"] = f
    frac = kelly_frac * f
    if frac > max_frac:
        frac, out["capped_by"] = max_frac, "max stake"
    n = math.floor(bankroll * frac / cost)
    if avail is not None and n > math.floor(avail):
        n, out["capped_by"] = math.floor(avail), "size at ask"
    if n >= 1:
        fee = kalshi_fee(ask, n, rate, multiplier)  # real fee is rounded up per order
        cost = ask + fee / n
        ev = p - cost
        ok = ev > 0  # on tiny orders the round-up-to-a-cent fee can erase the edge: then pass
        out.update(contracts=n if ok else 0, stake=n * ask + fee if ok else 0.0, fee_per=fee / n,
                   cost_per=cost, ev_per=ev, edge_pct=100 * ev / cost,
                   capped_by=out["capped_by"] if ok else "fee rounding")
    return out
