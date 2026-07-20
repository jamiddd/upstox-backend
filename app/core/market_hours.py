from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

# Mon-Fri, 09:15-15:30 IST -- the NSE cash/F&O normal market session. Mirrors the Android client's
# own `MarketHours.kt` (used there to skip polling/the live feed for data that provably can't
# change while the market's shut); this is the same check, server-side, so the tracked-instrument
# background poller (see UnderlyingSignalsService's module doc / the poller in app.main) doesn't
# burn Upstox API calls outside real trading hours either. Does **not** account for NSE trading
# holidays -- there's no holiday calendar anywhere in this app today, so a holiday just falls back
# to today's existing behavior (a few extra no-op ticks), not a regression, same posture as the
# Android side.
_ZONE = ZoneInfo("Asia/Kolkata")
_OPEN = time(9, 15)
_CLOSE = time(15, 30)


def is_market_open(now: datetime | None = None) -> bool:
    """Whether the NSE normal market session is open right now (or at [now], if given -- an
    injectable override so tests aren't tied to the wall clock, same pattern as this file's own
    callers use for `monotonic()`)."""
    local = (now or datetime.now(_ZONE)).astimezone(_ZONE)
    if local.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    local_time = local.time()
    return _OPEN <= local_time < _CLOSE
