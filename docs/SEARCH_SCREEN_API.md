# Search Screen API

Backend contract for searching option-capable underlyings.

All endpoints require:

```text
X-API-Key: <MOBILE_API_KEY>
```

## Search Underlyings

```http
GET /api/search/underlyings
GET /api/search/underlyings?query=nifty
GET /api/search/underlyings?query=reliance&limit=10&page_number=1
```

Query params:

```text
query optional, 0-50 characters
limit optional, 1-30, default 20
page_number optional, starts at 1, default 1
```

When `query` is empty, the backend returns a default paginated list of index underlyings that provide options.

When `query` has a value, the backend searches current-month CE/PE option contracts and returns only deduped underlyings that are valid for the option trading flow:

```text
INDEX underlyings
EQUITY underlyings with F&O option contracts
```

Response:

```json
{
  "query": "nifty",
  "results": [
    {
      "instrument_key": "NSE_INDEX|Nifty 50",
      "symbol": "NIFTY",
      "name": "Nifty 50",
      "underlying_type": "INDEX",
      "exchange": "NSE",
      "lot_size": 65.0,
      "freeze_quantity": 1755.0,
      "tick_size": 0.05
    },
    {
      "instrument_key": "NSE_EQ|INE002A01018",
      "symbol": "RELIANCE",
      "name": "RELIANCE INDUSTRIES LTD",
      "underlying_type": "EQUITY",
      "exchange": "NSE",
      "lot_size": 500.0,
      "freeze_quantity": 10000.0,
      "tick_size": 0.05
    }
  ],
  "page": {
    "page_number": 1,
    "records": 20,
    "total_records": 2,
    "total_pages": 1
  }
}
```

When the user selects a result, pass `instrument_key` back to the main screen as its `underlying_key`.

Use `lot_size`, `freeze_quantity`, and `tick_size` to guide the order UI:

```text
lot_size: quantity stepper increment/decrement
tick_size: price stepper and client-side price validation
freeze_quantity: show when the backend will split an order into slices
```

## Upstox Mapping

The backend calls Upstox instrument search with:

```text
segments=FO
instrument_types=CE,PE
expiry=current_month
atm_offset=0
records=<limit>
```

It uses option rows only as proof that the underlying is option-capable; it does not return option contracts from this endpoint.
