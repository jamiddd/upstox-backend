# Watchlist API

Backend contract for the ticker watchlists (India, Global) shared by the Android app's Main
screen ticker and the web client's TickerBar/WatchlistScreen.

Both endpoints below are on `dual_router` (`require_mobile_or_web`), so either the mobile API key
or the web client's session cookie works:

```text
X-API-Key: <MOBILE_API_KEY>
```

or a valid `psw_session` cookie (see the web client's `/api/auth/web-login`).

## Get Watchlist

```http
GET /api/user/watchlist/india
GET /api/user/watchlist/global
```

Returns the current persisted list for `list_id` (`india` or `global`), or an empty list if
nothing's been saved yet:

```json
{
  "items": [
    { "instrument_key": "NSE_INDEX|Nifty 50", "symbol": "NIFTY 50", "lot_size": 25, "is_underlying": true },
    { "instrument_key": "NSE_INDEX|Nifty Bank", "symbol": "BANKNIFTY", "lot_size": 15, "is_underlying": true }
  ]
}
```

## Set Watchlist

```http
PUT /api/user/watchlist/india
PUT /api/user/watchlist/global
```

Replaces the whole persisted list for `list_id` (not an incremental add/remove -- the client
always sends its full current list, same contract as `PUT /api/user/tracked-instruments`). Both
Android and the web client push their full current list here right after every local add/remove/
reorder; there is no conflict resolution -- last write wins, same posture as every other
whole-list-replace endpoint in this backend.

```http
PUT /api/user/watchlist/india
{"items": [{"instrument_key": "NSE_INDEX|Nifty 50", "symbol": "NIFTY 50", "lot_size": 25, "is_underlying": true}]}
```

Response echoes the freshly-saved list back (same shape as `GET`):

```json
{
  "items": [
    { "instrument_key": "NSE_INDEX|Nifty 50", "symbol": "NIFTY 50", "lot_size": 25, "is_underlying": true }
  ]
}
```

Persisted to a small flat JSON file (`WATCHLIST_PATH`, default `/data/watchlist.json`), covered by
the same Docker volume as the other flat-file stores (`TRACKED_INSTRUMENTS_PATH`,
`MAX_LOSS_SETTINGS_PATH`).
