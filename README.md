# Prop Advisor

Local dashboard that compares Kalshi NBA/NFL player-prop contracts with vig-free sharp sportsbook
prices and ranks the bets that are still +EV after Kalshi's fee. It advises only: it never places orders.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

With no key it runs in **Kalshi-only mode**: live Kalshi prices, market-implied win %, and injuries, but no
sportsbook fair value. For the full +EV board, copy `.env.example` to `.env`, set `ODDS_API_KEY`
(from the-odds-api.com) and restart. Set `DEMO_MODE=1` to see every panel with simulated data.
Kalshi market data, ESPN injuries and Open-Meteo weather need no keys.

Tests: `python -m unittest discover -s tests -v`

## What each tab shows

| Tab | Signal |
|---|---|
| Top +EV bets | Exact player + stat + line match. Fair value = devigged sharp odds. Net edge after fee, Quarter-Kelly size. |
| Steam & stale quotes | Sharp fair value moved 3+ points (or the main line moved) in the last 10 minutes while the Kalshi price on the side to buy has not followed. |
| Injuries, usage & weather | ESPN status changes, teammates' props flagged for usage/target spikes, Kalshi markets still open on players ruled out, wind/rain flags for outdoor NFL games. |
| Diagnostics | Feed health, which Kalshi series are being polled, markets that found no exact sharp line. |

## The maths

- **Line mapping.** Kalshi "25+ points" YES is the same bet as sportsbook Over 24.5. Only exact line matches are priced; nothing is interpolated.
- **Devig.** Multiplicative by default (power and additive selectable) on the first book in `SHARP_BOOKS` that quotes both sides of that line.
- **Fee.** Taker fee = round up(0.07 x contracts x P x (1-P)) to the cent, per order.
- **Net edge %** = (fair value - price - fee) / (price + fee), buying at the ask.
- **Size.** Full Kelly f* = (p - cost) / (1 - cost); stake = 0.25 x f* x bankroll, capped at 2% of bankroll and at the contracts resting at the ask.
- **Liquidity filter.** Hides markets with a spread over 5c, a one-sided book, or fewer than 10 contracts at the ask (all adjustable in the sidebar).

## Things to check on first live run

1. **Kalshi series tickers.** The defaults in `advisor/config.py` (`KXNBAPTS`, `KXNFLPASSYDS`, ...) are best guesses. The app
   also auto-discovers series by title. Open Diagnostics: any series showing 0 markets is wrong. Add the right ones in `.env`
   as `KALSHI_SERIES=NBA:points:TICKER,NFL:pass_yds:TICKER`.
2. **Odds API quota.** Player props are billed per market per game on every call. Polling ten games every 60s can use
   thousands of credits an hour, so the odds feed defaults to 60s (Kalshi and injuries to 20-30s). Tune
   `ODDS_POLL_SECONDS`, `MAX_EVENTS_PER_POLL`, `HORIZON_HOURS`, `INCLUDE_ALTERNATES`. Credits left are shown on the dashboard.
3. **Sharp book coverage.** Pinnacle prices fewer props than US books, and The Odds API may not carry Circa or Bookmaker.
   Edit `SHARP_BOOKS` to the keys your plan returns. The consensus fallback (sidebar) uses soft books and is a weaker baseline.
4. **Settlement rules.** Kalshi props can settle differently from sportsbooks when a player does not play
   (books void, Kalshi may not). Props on Out/Doubtful players are hidden by default.

## Limits of the signals

- The usage and weather flags are directional notes, not probability adjustments. The edge number comes only from the sharp price.
- "Key teammate" is not measured: any newly Out/Doubtful teammate (NBA) or QB/RB/WR/TE (NFL) raises a flag.
- Steam history lives in memory and starts empty on each launch.
- Weather assumes the home team's own stadium; neutral-site games will be wrong.
- Size at the ask comes from the market summary's top-of-book fields. If Kalshi omits them, the row is flagged "size at ask unknown".

## Layout

```
app.py               Streamlit dashboard
advisor/engine.py    background polling + matching
advisor/kalshi.py    Kalshi client, threshold parsing
advisor/odds.py      The Odds API client (swap in another feed here)
advisor/injuries.py  ESPN injuries/rosters + change monitor
advisor/weather.py   Open-Meteo + stadium table
advisor/quant.py     devig, fee, EV, Kelly
advisor/signals.py   steam detection, usage/weather flags
advisor/recs.py      ranked recommendation rows
advisor/demo.py      simulated feeds
```

This is a decision-support tool, not financial advice. Check that trading these contracts is permitted where you are.
