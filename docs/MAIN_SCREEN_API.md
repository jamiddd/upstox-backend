# Main Screen API

Backend contract for the app's option trading main screen.

All endpoints require:

```text
X-API-Key: <MOBILE_API_KEY>
```

## Bootstrap

```http
GET /api/main/bootstrap
GET /api/main/bootstrap?underlying_key=NSE_INDEX%7CNifty%2050&expiry_date=2026-07-16
```

Defaults:

```text
underlying_key=NSE_INDEX|Nifty 50
expiry_date=<nearest available expiry>
```

Returns the underlying, available expiries, account summary, and currently open positions.
`previous_close` is the underlying's last trading day's closing price, letting the app show a
"(+0.40%)" change badge next to the spot price.

FIX: this is fetched directly from the daily candle endpoint (the most recent *completed*
session's own close, `to_date` = yesterday) -- not derived from a live quote's `net_change` field
(`last_price - net_change`), which an earlier version of this endpoint used. That derivation
trusted `net_change` to always be the signed change from yesterday's close, which isn't reliable
enough to build a change-*direction* indicator on: it silently produced a wrong-but-plausible
previous close whenever a quote's `net_change` didn't behave exactly that way (most visibly right
around a gap-open), which showed up as the app's "(+x.xx%)" badge reading the wrong direction
entirely -- e.g. "+0.5%" on a day that gapped down. The change badge is always just
`(spot_price - previous_close) / previous_close`; the fix is entirely in how `previous_close`
itself is sourced, not the formula. Cached per (instrument, day) server-side, since a previous
close is fixed for the whole trading day.

```json
{
  "underlying": {
    "instrument_key": "NSE_INDEX|Nifty 50",
    "symbol": "NIFTY",
    "name": "NIFTY",
    "spot_price": 25050.0,
    "previous_close": 25050.0
  },
  "expiries": ["2026-07-16", "2026-07-23"],
  "selected_expiry": "2026-07-16",
  "summary": {
    "opening_balance": 100000.0,
    "profit_loss": 400.0,
    "closing_balance": 102300.0,
    "available_margin": 99980.0,
    "margin_used": 10000.0,
    "payin_amount": 1900.0
  },
  "open_positions": [
    {
      "instrument_key": "NSE_FO|111",
      "trading_symbol": "NIFTY26JUL25000CE",
      "quantity": 75.0,
      "entry_price": 120.0,
      "last_price": 125.0,
      "pnl": 375.0
    }
  ]
}
```

## Selected Strike Quote

```http
GET /api/main/selected-quote?expiry_date=2026-07-16&strike_price=25000&option_type=CE
```

Optional:

```text
underlying_key=NSE_INDEX|Nifty 50
```

The app sends the selected strike and CE/PE toggle. The backend resolves the matching option contract and returns only the values needed for the spot, buy, and sell text views.

```json
{
  "underlying": {
    "instrument_key": "NSE_INDEX|Nifty 50",
    "spot_price": 25050.0
  },
  "contract": {
    "instrument_key": "NSE_FO|111",
    "trading_symbol": "NIFTY26JUL25000CE",
    "strike_price": 25000.0,
    "option_type": "CE",
    "lot_size": 65.0,
    "freeze_quantity": 1755.0,
    "tick_size": 0.05,
    "ltp": 125.0,
    "bid_price": 124.5,
    "ask_price": 125.5
  }
}
```

## Option Chain

```http
GET /api/main/option-chain?expiry_date=2026-07-16&underlying_key=NSE_INDEX|Nifty 50
```

Returns every listed strike's **live** CE/PE market data and option greeks for the given
underlying + expiry -- powers the app's smart strike selector (ATM/delta-target/liquidity/
manual-offset/DTE-aware modes all pick from this same per-strike data, client-side; see
`ui/main/strikeselection/StrikeSelector.kt` in the app repo).

FIX: this used to wrap Upstox's `/option/contract` endpoint, which only returns bare contract
metadata (instrument key, lot size, tick size) -- no LTP, no bid/ask, no OI, no greeks, so there
was no data to build anything "smart" from. Now wraps Upstox's `/option/chain` endpoint instead,
which returns everything needed for every strike in one call. Cached much more briefly than
before (15s, not 600s) since this data is live-changing throughout the day, not static.

```json
{
  "underlying_key": "NSE_INDEX|Nifty 50",
  "expiry_date": "2026-07-16",
  "underlying_spot_price": 25050.0,
  "strikes": [
    {
      "strike_price": 25000.0,
      "ce": {
        "instrument_key": "NSE_FO|111",
        "ltp": 125.0,
        "bid_price": 124.5,
        "ask_price": 125.5,
        "bid_qty": 300.0,
        "ask_qty": 450.0,
        "oi": 1250000.0,
        "prev_oi": 1180000.0,
        "volume": 5400000.0,
        "delta": 0.52,
        "gamma": 0.0012,
        "theta": -18.4,
        "vega": 12.1,
        "iv": 14.2
      },
      "pe": {
        "instrument_key": "NSE_FO|222",
        "ltp": 90.0,
        "bid_price": 89.5,
        "ask_price": 90.5,
        "bid_qty": 200.0,
        "ask_qty": 350.0,
        "oi": 980000.0,
        "prev_oi": 1020000.0,
        "volume": 4100000.0,
        "delta": -0.47,
        "gamma": 0.0012,
        "theta": -17.9,
        "vega": 12.0,
        "iv": 13.9
      }
    }
  ]
}
```

A strike missing a listed CE or PE contract simply omits that key (e.g. deep ITM/OTM strikes
sometimes only have one side listed).

## OI Analysis

```http
GET /api/market/oi-analysis?instrument_key=NSE_INDEX%7CNifty%2050&expiry=current_week&date=2026-07-17&change_interval=1&bucket_interval=60
```

Returns Upstox's four complementary open-interest analyses in one client request. The backend
calls `/market/oi`, `/market/change-oi`, `/market/max-pain`, and `/market/pcr` concurrently and
caches the combined response for 60 seconds. The request fails atomically if any upstream call
fails or returns a malformed data object, so the client never mistakes partial analysis for a
complete result.

Query parameters:

- `instrument_key`: underlying asset key; defaults to `NSE_INDEX|Nifty 50`.
- `expiry`: required expiry date (`YYYY-MM-DD`) or Upstox relative keyword such as
  `current_week`, `next_week`, or `current_month`.
- `date`: required analysis date in `YYYY-MM-DD` format.
- `change_interval`: positive number of days used for Change in OI; defaults to `1`.
- `bucket_interval`: positive intraday bucket size in minutes used by Max Pain and PCR;
  defaults to `60`.

```json
{
  "instrument_key": "NSE_INDEX|Nifty 50",
  "expiry": "2026-07-23",
  "date": "2026-07-17",
  "change_interval": 1,
  "bucket_interval": 60,
  "oi": {
    "total_puts": 12500000,
    "total_calls": 9800000,
    "spot_closing_price": 25050.0,
    "expiry": "2026-07-23",
    "call_put_oi_data_list": [
      {"strike_price": 25000.0, "call_oi": 1250000, "put_oi": 980000}
    ]
  },
  "change_oi": {
    "total_put_change_oi": 2500000,
    "total_call_change_oi": -1800000,
    "call_put_oi_data_list": [
      {"strike_price": 25000.0, "call_change_oi": -120000, "put_change_oi": 350000}
    ]
  },
  "max_pain": {
    "max_pain": 25000.0,
    "insights": [{"max_pain": 25000.0, "spot_price": 25040.0, "time": "15:15"}]
  },
  "pcr": {
    "pcr": 1.2755,
    "insights": [{"pcr": 1.27, "spot_price": 25040.0, "time": "15:15"}]
  }
}
```

PCR's headline value can be derived from total put/call OI, and current max pain can be
calculated from the strike series. Their dedicated Upstox APIs are still used because they also
provide intraday insight history; Change in OI likewise requires a comparison snapshot that is
not present in a single OI response.

## Position Quotes

```http
GET /api/main/position-quotes?instrument_keys=NSE_FO%7C111,NSE_FO%7C222
```

Returns compact LTP + previous-close snapshots for any instrument keys -- originally for open
positions, but it's a generic quote call, also used to poll the toolbar's watchlist ticker
(regular NSE/BSE instruments AND Upstox's Global Instruments, e.g. `GLOBAL_INDEX|^GSPC` for S&P
500, `GLOBAL_INDEX|SGX NIFTY` for GIFT NIFTY, `GLOBAL_INDICATOR|USDINR` -- see Upstox's Global
Instruments file; the underlying Full Market Quote call supports these directly). `previous_close`
lets the app color each entry by direction (up/down vs. yesterday's close).

```json
{
  "positions": [
    {
      "instrument_key": "NSE_FO|111",
      "ltp": 125.0,
      "previous_close": 120.0
    }
  ]
}
```

## Summary

```http
GET /api/main/summary
```

```json
{
  "opening_balance": 100000.0,
  "profit_loss": 400.0,
  "closing_balance": 102300.0,
  "available_margin": 99980.0,
  "margin_used": 10000.0,
  "payin_amount": 1900.0
}
```

All six fields' source paths are confirmed against a real `GET /v3/user/get-funds-and-margin` response (see "Raw Funds and Margin" below):

- `opening_balance` (`available_to_trade.cash_available_to_trade.cash.opening_balance`) is a genuine static start-of-day snapshot -- it does NOT move when cash is added/withdrawn intraday.
- `profit_loss` sums *every* position Upstox returns for the day, including ones already squared off (quantity 0) -- not just currently-open ones -- since a closed position still carries its realized P&L here; summing only open positions would read as 0 on a day that was all open-and-close scalps with nothing left open.
- `available_margin` (`available_to_trade.total`) is the actual "can I place another order right now" number: cash + pledge margin, already net of margin blocked by open positions and today's cash movement.
- `margin_used` sums `cash_available_to_trade.margin_used.total` and `pledge_available_to_trade.margin_used.total` -- margin currently locked by open positions.
- `payin_amount` is `cash.added_today + cash.withdrawn_today` (the latter already negative) -- net cash movement today.
- `closing_balance` = `opening_balance + payin_amount + profit_loss` -- includes today's net cash movement (not just `opening_balance + profit_loss` as before), since a mid-day deposit is real money added to the account, not "profit".

## Underlying Signals

```http
GET /api/main/underlying-signals
GET /api/main/underlying-signals?underlying_key=NSE_INDEX%7CNifty%2050
GET /api/main/underlying-signals?underlying_key=NSE_INDEX%7CNifty%2050&expiry_date=2026-07-23
GET /api/main/underlying-signals?underlying_key=NSE_INDEX%7CNifty%2050&underlying_symbol=NIFTY
```

Glanceable technical-analysis tags for the underlying -- shown to the app's user just before they
place a strike order, so they can see e.g. "is the underlying above its 9 EMA" without leaving the
trading screen. Deliberately computed on the **underlying's** own price action (spot/futures),
not the option contract about to be traded -- an option premium is dominated by theta decay and
IV changes rather than the underlying's own trend/momentum, so an EMA/ATR/opening-range reading
on the premium itself would be meaningless here.

`expiry_date` is optional -- when given, the response also includes PCR/max-pain/OI-support/
OI-resistance tags (see `pcr`/`max_pain`/`oi_support`/`oi_resistance` below), reusing the existing
OI Analysis endpoint's own service and 60s cache under the hood (see "OI Analysis" above).
Omitting it just skips those four fields/tags; everything else is unaffected.

`underlying_symbol` is likewise optional -- when given, the response also includes a VWAP tag/
field (see `vwap` below), computed from the underlying's own current-month futures contract
(resolved via a symbol-text instrument search, since Upstox has no search-by-instrument_key mode
and this backend has no reliable way to derive a search-safe symbol from an arbitrary
`instrument_key` -- ISIN-keyed equities in particular). Omitting it just skips the VWAP field/tag;
everything else is unaffected.

```json
{
  "ltp": 25050.0,
  "ema9_5m": {"value": 25010.5, "position": "above"},
  "ema9_15m": {"value": 24990.0, "position": "above"},
  "atr14_5m": 42.3,
  "opening_range": {"window_minutes": 15, "high": 25080.0, "low": 24990.0, "position": "inside"},
  "previous_day": {"high": 25100.0, "low": 24900.0, "close": 24980.0},
  "pivots": {"p": 24993.3, "r1": 25086.6, "r2": 25193.3, "s1": 24886.6, "s2": 24793.3},
  "round_step": 50.0,
  "today_open": 24950.0,
  "no_trade_zone": false,
  "nearest_level": {"label": "R1 Pivot", "value": 25086.6, "distance_percent": 0.15},
  "nearest_or_target": null,
  "pcr": {"value": 1.35, "bias": "bullish"},
  "max_pain": {"value": 25000.0, "pull": "bearish"},
  "oi_support": {"value": 24900.0, "oi": 1200000.0},
  "oi_resistance": {"value": 25200.0, "oi": 1500000.0},
  "vwap": {"value": 25040.25, "position": "above"},
  "tags": [
    "Above 5m EMA9 by 39.50 (15m Above by 60.00)",
    "Inside opening range",
    "Near R1 Pivot by 36.60",
    "PCR 1.35 - Bullish bias",
    "Max Pain 25000 by +50.00 - Bearish pull",
    "OI Support 24900 by +150.00",
    "OI Resistance 25200 by -150.00",
    "Above VWAP by 9.75"
  ]
}
```

`pcr`/`max_pain`/`oi_support`/`oi_resistance` (and their tags) are only present when `expiry_date`
was given -- all `null` otherwise, or if Upstox's OI endpoints fail for that expiry (this degrades
quietly, same as `main/summary`'s funds-unavailable handling -- the rest of the response is
unaffected).

A breakout that's also sitting on one of the opening range's own measured-move target levels
looks like this instead (`opening_range.position` `"above"`, LTP right on "OR Target 1"):

```json
{
  "opening_range": {"window_minutes": 15, "high": 25100.0, "low": 25000.0, "position": "above"},
  "nearest_or_target": {"label": "OR Target 1", "value": 25150.0, "distance_percent": 0.03},
  "tags": ["Above opening range by 50.00 (near OR Target 1 by +0.30, caution: possible pullback)"]
}
```

- `ema9_5m` / `ema9_15m`: 9-period EMA of closes on 5-minute and 15-minute candles respectively --
  the 5m read is meant for scalping timing, the 15m read for the broader intraday bias. `position`
  is `"above"`/`"below"`/`"at"` LTP relative to the EMA value; either can be `null` (with `value`
  also `null`) if there isn't yet enough candle history to compute it.
- `atr14_5m`: 14-period Average True Range (Wilder's smoothing) on the 5-minute series -- a
  volatility gauge, in underlying price units (e.g. NIFTY points).
- `opening_range`: the high/low of the first 15 minutes of today's session (9:15-9:30 IST, the
  first three 5-minute candles), and where LTP currently sits relative to that range
  (`"above"`/`"below"`/`"inside"`).
- `previous_day`: the prior completed trading session's high/low/close, straight off the daily
  candle series.
- `pivots`: classic pivot points (`p`/`r1`/`r2`/`s1`/`s2`) computed from `previous_day`'s
  high/low/close.
- `round_step`: the underlying's own strike spacing (the most common gap between consecutive
  option-chain strikes for this underlying), used to find the two round psychological numbers
  bracketing LTP. `0.0` if there isn't enough strike data to derive a step.
- `today_open`: today's session open (the first 5-minute candle's open). `null` before any candle
  for today exists yet.
- `no_trade_zone`: `true` when LTP is currently within a fixed **15 points** of `today_open` --
  price whipsaws right around the open before it's picked a direction, so this is a caution not to
  act on the rest of the bulletin yet. Always `false` (never a false caution) when `today_open`
  isn't known yet. When `true`, a `"No-Trade Zone -- within 15 of Day Open (X)"` tag is inserted
  **first** in `tags`, ahead of every other tag -- see the `tags` description below.
- `nearest_level`: whichever of `previous_day`'s three values, the five pivot levels, or the two
  round numbers is closest to LTP, **only if** it's within 0.15% of LTP -- `null` if nothing is
  that close right now.
- `nearest_or_target`: **only** computed once `opening_range.position` is `"above"` or `"below"`
  (a genuine breakout -- `null` for `"inside"` or unknown). Four measured-move target levels are
  projected beyond whichever side broke out, as multiples of the OR's own size (high - low): "OR
  Target 1" = breakout side +/- 0.5x the OR, "OR Target 2" = 1x, "OR Target 3" = 1.5x, "OR Target
  4" = 2x. `nearest_or_target` is whichever of those four LTP is currently closest to, if within
  0.15% of LTP -- `null` if none are that close. A breakout past the OR is a genuinely bullish/
  bearish signal on its own; sitting right on one of these targets too doesn't contradict that,
  it just adds a "this exact level has historically tended to see a stall/reversal" caution --
  folded straight into the *same* `"Above/Below opening range"` tag (not a separate one), with a
  signed (`+`/`-`) point distance to the target, e.g. `"Above opening range by 50.00 (near OR
  Target 1 by +0.30, caution: possible pullback)"`.
- `pcr`: `null` unless `expiry_date` was given (and Upstox's OI endpoints succeeded for it).
  `value` is put-call ratio computed **locally** from the per-strike `call_put_oi_data_list`
  (sum of put OI / sum of call OI), **restricted to the 5 listed strikes on each side of ATM**
  (11 strikes total, including ATM) -- not Upstox's own whole-chain `/market/pcr` value. This app
  is a scalping tool, so OI parked far from the money would otherwise dominate the ratio without
  being relevant to the trade actually being considered. `bias` is `"bullish"` (PCR >= 1.2 --
  heavy near-the-money put writing reads as traders not expecting a fall), `"bearish"`
  (PCR <= 0.8), or `"neutral"` in between.
- `max_pain`: same availability as `pcr`, but **not** restricted to near-ATM strikes -- `value` is
  Upstox's own whole-chain max pain (the strike where option writers collectively lose the least
  by expiry, across every strike), since narrowing its inputs would just make it a different,
  wrong number rather than a more scalping-relevant one. Price tends to gravitate toward it as
  expiry approaches. `pull` is `"bullish"` if LTP is currently below it (expected pull up),
  `"bearish"` if above (pull down), `"neutral"` if exactly on it.
- `oi_support` / `oi_resistance`: same availability as `pcr`, and same near-ATM restriction (the 5
  strikes on each side of ATM) computed from the same per-strike `call_put_oi_data_list`.
  `oi_support` is the strike with the single highest **put** OI *within that window* (heavy put
  writing there reads as a level put writers will defend, i.e. support); `oi_resistance` is the
  strike with the highest **call** OI in the same window (the mirror image). `oi` on each is that
  strike's own OI count. `null` if there's no usable per-strike data within the window.
- `vwap`: `null` unless `underlying_symbol` was given **and** a current-month futures contract is
  listed for this underlying (true for NIFTY/BANKNIFTY-style indices and most F&O-enabled stocks,
  false for most individual equities and any Upstox resolution failure -- degrades quietly, same
  posture as `pcr`/`max_pain`). **SENSEX is a special case**: it has no futures market on Upstox at
  all, so its VWAP always resolves against **Nifty's own futures contract** instead. `value` is the
  session VWAP (cumulative typical-price-times-volume / cumulative volume, today's candles only) of
  the resolved futures contract; `position` is `"above"`/`"below"`/`"at"` the *futures* contract's
  own LTP relative to it (not the underlying's LTP, since VWAP itself is a futures-contract-only
  concept here -- the index has no traded volume of its own to compute VWAP from).
- `tags`: a small set of ready-to-render short labels (e.g. `"Above 5m EMA9 by 39.50 (15m Above by
  60.00)"`, `"ATR 42.3"`, `"Near R1 Pivot by 36.60"`, `"PCR 1.35 - Bullish bias"`, `"OI Support
  24900 by +150.00"`) built from the fields above -- the client can display these directly without
  any string-building of its own. The 5m and 15m EMA reads share a single line -- the 5m read (the
  one meant for scalping timing) drives the line's leading `"Above"`/`"Below"`, with the 15m read
  parenthesized alongside it; when only one of the two has enough candle history yet, that one
  appears alone, unparenthesized. Every directional tag (EMA above/below, opening-range
  above/below, a nearby level, PCR bias, max-pain pull, OI support/resistance) spells out its
  magnitude, not just the direction -- `ATR` and `"Inside opening range"` are the only two with no
  direction/distance to report. The PCR/max-pain tags say `"Bullish"`/`"Bearish"` explicitly (rather than starting
  with `"Above"`/`"Below"` like the others) since neither phrasing fits those two signals -- the
  app's tag-sentiment classifier checks for both. The OI support/resistance tags are informational
  (no bullish/bearish framing) -- they render as neutral on the client. When `no_trade_zone` is
  `true`, its `"No-Trade Zone -- within 15 of Day Open (X)"` tag is always **first** in the list --
  the client's tag-sentiment classifier renders it as a distinct warning (not bullish/bearish/
  neutral) so it doesn't get lost among the rest.

Candle-derived values (the EMAs, ATR, opening range, previous-day/pivots, round step) are cached
~60 seconds -- they only meaningfully change when a new candle closes, not on every feed tick.
`ltp` and everything computed relative to it (`position` fields, `nearest_level`) are read fresh
on every call.

## USD/INR (non-Upstox)

```http
GET /api/market/usd-inr
```

Response:

```json
{
  "ltp": 96.27,
  "previous_close": 96.335
}
```

Sourced from Yahoo Finance's unofficial chart endpoint, **not** Upstox -- Upstox's own quotes/LTP
endpoints reject USD INR outright. This is a best-effort, roughly-current value, not an accurate or
official rate: no error is ever returned, both fields are simply `null` if Yahoo is unreachable or
its response shape changes. Cached server-side for 60 seconds regardless of how often this is
called. No Upstox access token needed -- this route doesn't touch the user's Upstox account.

## Raw Funds and Margin

```http
GET /api/user/get-funds-and-margin
```

Returns the complete Upstox V3 `/user/get-funds-and-margin` response without reshaping it. Use
`/api/main/summary` when the screen only needs the normalized summary fields.

## Refresh Cadence

The backend keeps short in-memory caches to avoid excessive Upstox calls:

```text
selected/position quote data: ~0.75 seconds
positions: ~1 second
summary: ~5 seconds
option contracts/expiries: ~10 minutes
OI analysis: ~60 seconds
underlying-signals candle-derived values (EMAs/ATR/opening range/pivots): ~60 seconds
```

For millisecond-level flashing values, the next backend step should be a market data WebSocket bridge or authorization endpoint.

## Live Market Feed

For the lowest-latency bid/ask, spot, and position LTP updates, use Upstox Market Data Feed V3 instead of REST polling.

First request a one-time WebSocket URL:

```http
GET /api/market/feed/authorize
```

Response:

```json
{
  "status": "success",
  "data": {
    "authorized_redirect_uri": "wss://..."
  }
}
```

The app should connect directly to `authorized_redirect_uri`. The URL is single-use, so request a new one for every WebSocket connection attempt.

Subscribe with Upstox's binary/protobuf V3 request format. For this screen:

```json
{
  "guid": "<client-generated-id>",
  "method": "sub",
  "data": {
    "mode": "full",
    "instrumentKeys": [
      "NSE_INDEX|Nifty 50",
      "NSE_FO|<selected_contract_key>"
    ]
  }
}
```

Use `full` for the selected option contract because the buy/sell buttons need best bid/ask. The selected contract feed includes:

```text
fullFeed.marketFF.ltpc.ltp
fullFeed.marketFF.marketLevel.bidAskQuote[0].bidP
fullFeed.marketFF.marketLevel.bidAskQuote[0].askP
```

For open positions, subscribe to their instrument keys. If only LTP is needed for local P&L, `ltpc` mode is enough:

```json
{
  "guid": "<client-generated-id>",
  "method": "sub",
  "data": {
    "mode": "ltpc",
    "instrumentKeys": [
      "NSE_FO|<position_1>",
      "NSE_FO|<position_2>"
    ]
  }
}
```

When the selected strike changes, unsubscribe the previous selected contract and subscribe the new contract in `full` mode. When positions open or close, update the `ltpc` subscriptions accordingly.
