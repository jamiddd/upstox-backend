# Price Chart API

## Candles

```text
GET /api/market/candles
```

Returns one chronological, de-duplicated OHLCV series for the Android app's TradingView
Lightweight Charts screen. The backend combines Upstox's completed-session historical endpoint
with its separate current-session intraday endpoint, so clients do not need to understand that
upstream split.

Protected with the standard `X-API-Key` header and the stored Upstox OAuth token.

### Query parameters

- `instrument_key`: required Upstox instrument key.
- `unit`: `minutes`, `hours`, or `days`; defaults to `minutes`.
- `interval`: defaults to `5`. Minutes support 1–300, hours support 1–5, and days requires 1.
- `from_date`: required ISO date, inclusive.
- `to_date`: required ISO date, inclusive.

`from_date` must not be after `to_date`. Maximum ranges follow the chart's supported
timeframes while staying within Upstox V3 retrieval limits:

- Minute intervals from 1 through 15: 31 days.
- Minute intervals above 15 and all hourly intervals: 90 days.
- Daily candles: 730 days (two years).

### Response

```json
{
  "instrument_key": "NSE_INDEX|Nifty 50",
  "unit": "minutes",
  "interval": 5,
  "timezone": "Asia/Kolkata",
  "candles": [
    {
      "timestamp": "2026-07-23T09:15:00+05:30",
      "open": 25000.0,
      "high": 25025.0,
      "low": 24990.0,
      "close": 25020.0,
      "volume": 1000,
      "open_interest": 0.0
    }
  ]
}
```

Rows are always oldest-first. Malformed upstream rows are ignored, and a duplicate timestamp from
the intraday response replaces the historical copy.
