# Brokerage API

Backend contract for estimating the brokerage and statutory charges of one proposed order.

All requests require:

```text
X-API-Key: <MOBILE_API_KEY>
```

## Estimate Brokerage

```http
GET /api/charges/brokerage?instrument_key=NSE_FO%7C111&quantity=75&product=I&transaction_type=BUY&price=125.5
```

Query parameters:

```text
instrument_key required, non-empty Upstox instrument key
quantity required, positive integer
product required, I|D|MTF
transaction_type required, BUY|SELL
price required, positive number
```

The backend passes `instrument_key` to Upstox as its upstream `instrument_token` parameter. It
returns Upstox's response unchanged, including the estimated `total`, `brokerage`, taxes, and
other charges.

```json
{
  "status": "success",
  "data": {
    "charges": {
      "total": 24.58,
      "brokerage": 20.0,
      "taxes": {
        "gst": 3.6,
        "stt": 0.75,
        "stamp_duty": 0.06
      },
      "other_charges": {
        "transaction": 0.12,
        "clearing": 0.0,
        "ipft": 0.03,
        "sebi_turnover": 0.02
      }
    }
  }
}
```

This is an estimate for a proposed order, not a record of charges already charged on an executed
trade. Upstox can also return a `dp_plan` field; its `min_expense` is a daily per-scrip sale cost
that is not included in the brokerage calculation.
