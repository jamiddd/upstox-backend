# Order History Screen API

Backend contract for the order history screen.

All endpoints require:

```text
X-API-Key: <MOBILE_API_KEY>
```

## Orders

```http
GET /api/orders/history
GET /api/orders/history?scope=today&page_number=1&page_size=20
GET /api/orders/history?scope=all&page_number=1&page_size=50
```

Query params:

```text
scope optional, today|all, default today
page_number optional, starts at 1, default 1
page_size optional, 1-500, default 20
segment optional for scope=all, default FO
start_date optional for scope=all, YYYY-MM-DD
end_date optional for scope=all, YYYY-MM-DD
```

`scope=today` uses Upstox's current-day order book. It includes complete, rejected, cancelled, and open current-day orders.

`scope=all` uses Upstox historical trades. Upstox exposes past executed trades, not a complete past order book with rejected/cancelled orders. The response marks this explicitly:

```json
{
  "scope": "all",
  "source": "historical_trades",
  "availability_note": "Upstox exposes past executed trades, not all past rejected/cancelled orders."
}
```

## Today Response

```json
{
  "scope": "today",
  "source": "order_book",
  "orders": [
    {
      "id": "order-newer",
      "instrument_key": "NSE_FO|222",
      "trading_symbol": "NIFTY26JUL25000PE",
      "transaction_type": "SELL",
      "order_type": "MARKET",
      "product": "I",
      "status": "rejected",
      "quantity": 75.0,
      "filled_quantity": 0.0,
      "pending_quantity": 0.0,
      "price": 0.0,
      "average_price": 0.0,
      "trigger_price": 0.0,
      "timestamp": "2026-07-13 09:25:00",
      "exchange_timestamp": "",
      "status_message": "Margin exceeded"
    }
  ],
  "categories": {
    "open": [],
    "complete": [],
    "cancelled": [],
    "rejected": [],
    "other": []
  },
  "page": {
    "page_number": 1,
    "page_size": 20,
    "total_records": 1,
    "total_pages": 1
  }
}
```

## All Response

```json
{
  "scope": "all",
  "source": "historical_trades",
  "availability_note": "Upstox exposes past executed trades, not all past rejected/cancelled orders.",
  "orders": [
    {
      "id": "trade-newer",
      "instrument_key": "NSE_FO|222",
      "trading_symbol": "NIFTY26JUL25000PE",
      "transaction_type": "SELL",
      "status": "complete",
      "quantity": 75.0,
      "price": 90.0,
      "amount": 6750.0,
      "exchange": "NSE",
      "segment": "FO",
      "option_type": "PE",
      "strike_price": "25000",
      "expiry": "2026-07-16",
      "trade_date": "2026-07-12"
    }
  ],
  "categories": {
    "open": [],
    "complete": [],
    "cancelled": [],
    "rejected": [],
    "other": []
  },
  "page": {
    "page_number": 1,
    "page_size": 50,
    "total_records": 1,
    "total_pages": 1
  },
  "filters": {
    "segment": "FO",
    "start_date": "2024-04-01",
    "end_date": "2026-07-13"
  }
}
```

The default historical date range starts from the beginning of the oldest of the last three financial years and ends today.
