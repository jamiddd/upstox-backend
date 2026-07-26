from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import anyio

from app.services.main_screen_service import MainScreenService, _last_trade_date


def test_last_trade_date_converts_epoch_millis_to_ist_calendar_date() -> None:
    # 2026-07-24 10:30:00 UTC == 2026-07-24 16:00:00 IST (a Friday afternoon trade).
    quote = {"last_trade_time": "1784889000000"}

    assert _last_trade_date(quote, fallback=date(2099, 1, 1)) == date(2026, 7, 24)


def test_last_trade_date_falls_back_when_missing_or_unparseable() -> None:
    fallback = date(2026, 7, 26)

    assert _last_trade_date({}, fallback=fallback) == fallback
    assert _last_trade_date({"last_trade_time": "not-a-number"}, fallback=fallback) == fallback


class _WeekendFrozenQuoteUpstoxService:
    """A quote whose `last_price`/`last_trade_time` are both frozen on Friday 2026-07-24 (the
    exchange has been closed since, e.g. a Saturday/Sunday check) -- and a daily-candle history
    with a real, distinct close for every one of the last few sessions, so a bug that resolves
    "previous close" back to that same frozen Friday session (instead of Thursday, the session
    actually before it) shows up as a wrong number rather than accidentally passing.
    """

    async def get_quotes(self, access_token: str, instrument_key: str) -> dict[str, Any]:
        return {
            "data": {
                "NSE_INDEX:Nifty 50": {
                    "instrument_token": "NSE_INDEX|Nifty 50",
                    "last_price": 23767.45,
                    # 2026-07-24T16:00:00+05:30 -- Friday's last trade, per a real captured quote.
                    "last_trade_time": "1784889000000",
                },
            },
        }

    async def get_historical_candle(
        self,
        access_token: str,
        instrument_key: str,
        *,
        unit: str,
        interval: str,
        to_date: str,
        from_date: Optional[str] = None,
    ) -> dict[str, Any]:
        assert unit == "days"
        # A real session every weekday from Mon 2026-07-20 through Fri 2026-07-24 -- Thursday's
        # close (23500.0) is what a correct fix must return; Friday's (23767.45, matching the
        # frozen live quote above) is what the pre-fix bug wrongly returned instead.
        all_candles = [
            ["2026-07-20T00:00:00+05:30", 23100.0, 23250.0, 23050.0, 23200.0, 400000],
            ["2026-07-21T00:00:00+05:30", 23200.0, 23350.0, 23150.0, 23300.0, 400000],
            ["2026-07-22T00:00:00+05:30", 23300.0, 23450.0, 23250.0, 23400.0, 400000],
            ["2026-07-23T00:00:00+05:30", 23400.0, 23550.0, 23350.0, 23500.0, 400000],
            ["2026-07-24T00:00:00+05:30", 23500.0, 23823.6, 23606.3, 23767.45, 400000],
        ]
        candles = [row for row in all_candles if row[0][:10] <= to_date]
        return {"status": "success", "data": {"candles": candles}}


def test_previous_close_walks_back_one_full_session_over_a_weekend() -> None:
    """The regression this covers: on a real weekend, `date.today()` is Sat/Sun (a non-trading
    day) while the live quote is still showing Friday's frozen last trade. Resolving
    previous_close from `date.today()` fetches "most recent completed session before today",
    which over a weekend is that *same* Friday session -- making previous_close equal the
    current price and every change badge read a flat 0.00% regardless of how Friday itself
    actually moved. Using the quote's own last_trade_time as the reference session instead must
    walk back to Thursday's close, not Friday's.
    """
    service = MainScreenService(_WeekendFrozenQuoteUpstoxService())

    async def run() -> dict[str, Any]:
        quotes = await service._quotes("token", ["NSE_INDEX|Nifty 50"])
        from app.services.main_screen_service import _find_quote

        quote = _find_quote(quotes, "NSE_INDEX|Nifty 50")
        session_date = _last_trade_date(quote, fallback=date(2026, 7, 26))
        return {
            "session_date": session_date,
            "previous_close": await service._fetch_previous_close(
                "token", "NSE_INDEX|Nifty 50", session_date,
            ),
        }

    result = anyio.run(run)

    assert result["session_date"] == date(2026, 7, 24)
    assert result["previous_close"] == 23500.0
