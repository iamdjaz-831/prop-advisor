"""Prop Advisor dashboard.  Run:  streamlit run app.py"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from advisor.config import LEAGUES, Settings
from advisor.engine import Engine
from advisor.recs import build_rows, kalshi_rows, ladder_arbs

st.set_page_config(page_title="Prop Advisor", page_icon="📈", layout="wide")


@st.cache_resource
def get_engine() -> Engine:
    """One engine (and one background polling thread) per server process."""
    return Engine(Settings.from_env(Path(__file__).parent)).start()


engine = get_engine()
cfg = engine.cfg

# ---------------- sidebar: risk rules ----------------
sb = st.sidebar
sb.header("Bankroll & sizing")
bankroll = sb.number_input("Bankroll ($)", min_value=10.0, value=1000.0, step=100.0)
kelly_frac = sb.slider("Kelly fraction", 0.05, 1.0, 0.25, 0.05, help="0.25 = Quarter-Kelly")
max_frac = sb.slider("Max stake per bet (% of bankroll)", 0.5, 10.0, 2.0, 0.5) / 100
min_edge = sb.slider("Minimum net edge after fees (%)", 0.0, 15.0, 1.5, 0.5)

sb.header("Liquidity & spread filter")
max_spread = sb.slider("Max Kalshi bid-ask spread (c)", 1, 15, 5) / 100
min_size = sb.number_input("Min contracts at the ask", min_value=0, value=10, step=5)
min_vol = sb.number_input("Min 24h volume (contracts)", min_value=0, value=0, step=100)
show_filtered = sb.checkbox("Show markets that fail the filter", value=False)
show_codes = sb.checkbox("Show Kalshi contract codes and raw quotes", value=False)

sb.header("Fair value")
method = sb.selectbox("Devig method", ["multiplicative", "power", "additive"])
allow_consensus = sb.checkbox("Fall back to multi-book consensus when no sharp book has the line", value=False)
leagues = sb.multiselect("Leagues", list(LEAGUES), default=list(LEAGUES))
hide_out = sb.checkbox("Hide props where the player is Out/Doubtful", value=True)
lookback_h = sb.slider("Injury-change lookback (hours)", 1, 24, 6)
refresh = sb.slider("Dashboard refresh (seconds)", 15, 30, 20)
sb.caption(f"Feeds poll in the background: Kalshi {cfg.kalshi_poll_s:.0f}s, odds {cfg.odds_poll_s:.0f}s, "
           f"injuries {cfg.injury_poll_s:.0f}s (set in .env).")


def age(ts: float | None, now: float) -> str:
    return "never" if not ts else f"{max(0, now - ts):.0f}s ago"


def kalshi_only(snap: dict, now: float) -> None:
    """Free mode: live Kalshi quotes + injuries. No sportsbook feed, so no fair value or edge."""
    krows = [r for r in kalshi_rows(snap, max_spread=max_spread, min_size=min_size, min_volume=min_vol) if r["League"] in leagues]
    tradable = [r for r in krows if not r["Filtered"]]
    arbs = [a for a in ladder_arbs(snap, cfg.fee_rate, cfg.fee_multiplier) if a["League"] in leagues]
    st.title("Prop Advisor: live Kalshi player props")
    st.info("KALSHI-ONLY MODE: live Kalshi prices and ESPN injuries. Without a sportsbook feed there is no fair value, "
            "so no +EV ranking or bet sizing. Add ODDS_API_KEY to .env to turn those on.")
    c = st.columns(4)
    c[0].metric("Kalshi prop markets", len(krows))
    c[1].metric("Pass liquidity filter", len(tradable))
    c[2].metric("Ladder mispricings", len(arbs))
    c[3].metric("Injury changes", sum(1 for ch in snap["changes"] if now - ch["ts"] < lookback_h * 3600))
    bad = [f"{k}: {v['msg']}" for k, v in snap["status"].items() if not v["ok"]]
    if bad:
        st.error("Feed errors: " + " | ".join(bad))
    st.caption(f"Updated {datetime.fromtimestamp(now):%H:%M:%S} | Kalshi {age(snap['status'].get('kalshi', {}).get('ts'), now)}"
               f" | injuries {age(snap['status'].get('injuries', {}).get('ts'), now)}")
    t_mk, t_arb, t_inj, t_diag = st.tabs(["Kalshi markets", "Ladder mispricings", "Injuries", "Diagnostics"])
    with t_mk:
        shown = krows if show_filtered else tradable
        if shown:
            cols = [k for k in shown[0] if k not in ("Filtered", "Kalshi Contract", "YES bid (c)", "YES ask (c)", "NO ask (c)", "Stat")]
            cols = ["Win % (market)", "Pick", "Market"] + [k for k in cols if k not in ("Win % (market)", "Pick", "Market")]
            if show_codes:
                cols += ["YES bid (c)", "YES ask (c)", "NO ask (c)", "Kalshi Contract"]
            if show_filtered:
                cols.append("Filtered")
            st.dataframe(pd.DataFrame(shown)[cols], hide_index=True, width="stretch", height=min(700, 60 + 35 * len(shown)),
                         column_config={"Win % (market)": st.column_config.NumberColumn("Win %", format="%.1f%%", width="small"),
                                        "Pick": st.column_config.TextColumn(width="small"),
                                        "Market": st.column_config.TextColumn(width="medium"),
                                        "Kalshi Contract": st.column_config.TextColumn(width="small")})
            st.caption("Win % (market) is the chance Kalshi's own prices imply for the Pick side (midpoint of bid and ask). "
                       "It is the market's estimate, not an independent prediction: a 90% pick costs about 90c to win $1.")
        else:
            st.info("No Kalshi prop markets pass the filter yet. Check the Diagnostics tab to see which series returned markets.")
    with t_arb:
        st.caption("Two thresholds on the same player and stat priced inconsistently: buying YES on the lower line and NO on the "
                   "higher line costs under $1 after fees, and at least one must pay $1. Rare; check both order sizes before acting.")
        if arbs:
            st.dataframe(pd.DataFrame(arbs), hide_index=True, width="stretch")
        else:
            st.info("None right now.")
    with t_inj:
        recent = [ch for ch in snap["changes"] if now - ch["ts"] < lookback_h * 3600 and ch["league"] in leagues]
        if recent:
            st.dataframe(pd.DataFrame([{"When": f"{(now - ch['ts']) / 60:.0f} min ago", "League": ch["league"], "Player": ch["name"],
                                        "Pos": ch["pos"], "Team": ch["team"], "From": ch["old"], "To": ch["new"], "Note": ch["comment"]}
                                       for ch in recent]), hide_index=True, width="stretch")
        else:
            st.info("No status changes in the lookback window.")
        flagged = [r for r in krows if r["Flags"]]
        if flagged:
            st.subheader("Kalshi markets on players listed Out / Doubtful / Questionable")
            st.dataframe(pd.DataFrame(flagged)[["Market", "Pick", "Win % (market)", "Cost (c)", "Flags"]], hide_index=True, width="stretch")
    with t_diag:
        st.dataframe(pd.DataFrame([{"Feed": k, "OK": v["ok"], "Last run": age(v["ts"], now), "Detail": v["msg"]}
                                   for k, v in snap["status"].items()]), hide_index=True, width="stretch")
        st.caption("Series with 0 markets or 'error' are not live tickers. Add the right ones with KALSHI_SERIES in .env.")
        st.dataframe(pd.DataFrame([{"League": lg, "Stat": stt, "Series": tk, "Open markets": str(snap["diag"]["series_counts"].get(tk, ""))}
                                   for (lg, stt), tks in snap["series"].items() for tk in tks]), hide_index=True, width="stretch")
        st.write(f"Markets parsed: {snap['counts']['kalshi']} (skipped {snap['diag']['unparsed']}).")


@st.fragment(run_every=refresh)
def live() -> None:
    snap = engine.snapshot()
    now = snap["now"]
    rows = build_rows(snap, bankroll=bankroll, kelly_frac=kelly_frac, max_frac=max_frac, method=method,
                      sharp_books=cfg.sharp_books, allow_consensus=allow_consensus, fee_rate=cfg.fee_rate,
                      fee_mult=cfg.fee_multiplier, max_spread=max_spread, min_size=min_size, min_volume=min_vol,
                      lookback_s=lookback_h * 3600, now=now)
    rows = [r for r in rows if r["League"] in leagues and not (hide_out and r["_out"])]
    liquid = [r for r in rows if not r["Filtered"]]
    picks = [r for r in liquid if r["Net Edge %"] >= min_edge and r["Contracts"] >= 1]
    stale_alerts = [a for a in snap["alerts"] if a["stale"] and a["league"] in leagues]

    if not snap["odds_enabled"]:
        kalshi_only(snap, now)
        return
    st.title("Prop Advisor: Kalshi player props vs sharp fair value")
    if snap["demo"]:
        st.warning("DEMO MODE: simulated players, prices and injuries. Add ODDS_API_KEY to .env for live data.")
    c = st.columns(6)
    c[0].metric("+EV bets", len(picks))
    c[1].metric("Best net edge", f"{picks[0]['Net Edge %']:.1f}%" if picks else "n/a")
    c[2].metric("Suggested exposure", f"${sum(r['Bet Size ($)'] for r in picks):,.0f}")
    c[3].metric("Kalshi props / matched", f"{snap['counts']['kalshi']} / {snap['counts']['matched']}")
    c[4].metric("Stale-vs-steam alerts", len(stale_alerts))
    c[5].metric("Odds API credits left", snap["quota"] or "n/a")
    bad = [f"{k}: {v['msg']}" for k, v in snap["status"].items() if not v["ok"]]
    if bad:
        st.error("Feed errors: " + " | ".join(bad))
    st.caption(f"Updated {datetime.fromtimestamp(now):%H:%M:%S} | Kalshi {age(snap['status'].get('kalshi', {}).get('ts'), now)}"
               f" | sharp odds {age(snap['status'].get('odds', {}).get('ts'), now)}"
               f" | injuries {age(snap['status'].get('injuries', {}).get('ts'), now)}")

    t_ev, t_steam, t_ctx, t_diag = st.tabs(["Top +EV bets", "Steam & stale quotes", "Injuries, usage & weather", "Diagnostics"])

    with t_ev:
        shown = picks + ([r for r in rows if r not in picks] if show_filtered else [])
        cols = ["League", "Player & Prop", "Price (c)", "Fair Value (c)", "Net Edge %", "Bet Size ($)",
                "Contracts", "EV/contract (c)", "Fee/contract (c)", "Spread (c)", "Size @ ask", "24h Vol", "Fair Source",
                "Book Odds (O/U)", "Flags", "Game", "Start", "Kalshi Contract"] + (["Filtered"] if show_filtered else [])
        if shown:
            st.dataframe(pd.DataFrame(shown)[cols], hide_index=True, width="stretch", height=min(700, 60 + 35 * len(shown)),
                         column_config={"Net Edge %": st.column_config.NumberColumn(format="%.2f%%"),
                                        "Bet Size ($)": st.column_config.NumberColumn(format="$%.2f"),
                                        "Flags": st.column_config.TextColumn(width="large"),
                                        "Kalshi Contract": st.column_config.TextColumn(width="small")})
            st.caption("Click a column header to sort. Price is the ask you would pay as a taker. Net edge = (fair value - price - fee) "
                       "/ (price + fee). Bet size = Kelly fraction x full Kelly x bankroll, capped by max stake and by the size "
                       "resting at the ask.")
        else:
            st.info("No bets clear the edge and liquidity filters right now.")

    with t_steam:
        st.caption(f"Sharp fair value moved at least {cfg.steam_min_dp * 100:.0f} points, or the main line moved, within the last "
                   f"{cfg.steam_window_s / 60:.0f} minutes. 'Stale' means the Kalshi price on the side to buy has not followed.")
        alerts = [a for a in snap["alerts"] if a["league"] in leagues]
        if alerts:
            edge = {r["Kalshi Contract"]: r["Net Edge %"] for r in rows}
            st.dataframe(pd.DataFrame([{
                "Stale?": "STALE" if a["stale"] else "repriced", "Market": a["market"], "Type": a["kind"], "Sharp move": a["detail"],
                "Move (pts)": a["move_pts"], "Minutes ago": round(a["mins"], 1), "Buy": f"{a['side']} {a['ticker']}",
                "Kalshi price then (c)": round(a["k_then"] * 100, 1), "Kalshi price now (c)": round(a["k_now"] * 100, 1),
                "Net Edge % now": edge.get(f"{a['side']} {a['ticker']}"),
            } for a in alerts]), hide_index=True, width="stretch")
        else:
            st.info("No sharp moves detected in the window yet. History builds up after the app has run for a few minutes.")

    with t_ctx:
        recent = [ch for ch in snap["changes"] if now - ch["ts"] < lookback_h * 3600 and ch["league"] in leagues]
        st.subheader("Status changes")
        if recent:
            st.dataframe(pd.DataFrame([{"When": f"{(now - ch['ts']) / 60:.0f} min ago", "League": ch["league"], "Player": ch["name"],
                                        "Pos": ch["pos"], "Team": ch["team"], "From": ch["old"], "To": ch["new"], "Note": ch["comment"]}
                                       for ch in recent]), hide_index=True, width="stretch")
        else:
            st.info("No status changes in the lookback window.")
        out_names = {k[1]: v for k, v in snap["injuries"].items() if v["bucket"] == "OUT"}
        open_out = [{"Player": out_names[n]["name"], "Kalshi market": m["kq"].ticker, "YES bid (c)": (m["kq"].yes_bid or 0) * 100,
                     "YES ask (c)": (m["kq"].yes_ask or 0) * 100}
                    for m in snap["matches"] for n in out_names if f" {n} " in m["kq"].text_norm]
        if open_out:
            st.subheader("Kalshi markets still open on players ruled OUT")
            st.caption("Read the market rules before trading these: settlement for a player who does not play differs from sportsbooks.")
            st.dataframe(pd.DataFrame(open_out), hide_index=True, width="stretch")
        st.subheader("Props with usage or weather flags")
        flagged = [r for r in rows if r["Flags"] and "size at ask unknown" != r["Flags"]]
        if flagged:
            st.dataframe(pd.DataFrame(flagged)[["Player & Prop", "Kalshi Contract", "Price (c)", "Fair Value (c)", "Net Edge %", "Flags"]],
                         hide_index=True, width="stretch")
        else:
            st.info("No flagged props.")
        if snap["weather"]:
            st.subheader("NFL outdoor game weather")
            st.dataframe(pd.DataFrame([{"Game": w["game"], "Wind (mph)": w["wind"], "Gusts (mph)": w["gust"], "Precip (in/hr)": w["precip"],
                                        "Temp (F)": w["temp"], "Flag": w["flag"]} for w in snap["weather"].values()]),
                         hide_index=True, width="stretch")

    with t_diag:
        st.subheader("Feeds")
        st.dataframe(pd.DataFrame([{"Feed": k, "OK": v["ok"], "Last run": age(v["ts"], now), "Detail": v["msg"]}
                                   for k, v in snap["status"].items()]), hide_index=True, width="stretch")
        st.subheader("Kalshi series polled")
        st.caption("A count of 0 or 'error' means that ticker is not a live series. Add the right ones with KALSHI_SERIES in .env.")
        st.dataframe(pd.DataFrame([{"League": lg, "Stat": stt, "Series": tk, "Open markets": str(snap["diag"]["series_counts"].get(tk, ""))}
                                   for (lg, stt), tks in snap["series"].items() for tk in tks]), hide_index=True, width="stretch")
        cts = snap["counts"]
        st.write(f"Kalshi prop markets parsed: {cts['kalshi']} (skipped {snap['diag']['unparsed']}). Player found in the sharp feed: "
                 f"{cts['named']}. Exact line match: {cts['matched']}. Sharp player lines: {cts['sharp_lines']}.")
        miss = [{"Kalshi market": m["kq"].title, "Ticker": m["kq"].ticker, "Kalshi line": m["kq"].line,
                 "Nearest sharp line": m["nearest_line"]} for m in snap["matches"] if m["sq"] is None]
        if miss:
            st.subheader("Kalshi markets without an exact sharp line")
            st.dataframe(pd.DataFrame(miss), hide_index=True, width="stretch", height=300)


live()
