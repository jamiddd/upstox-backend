from __future__ import annotations

from typing import Any, Optional

import anyio
import pytest

from app.core.exceptions import UpstoxApiError, UpstoxAuthRequiredError
from app.services import tracked_instruments_poller as poller


class _FakeTokenStore:
    def __init__(self, *, token: Optional[str] = "upstox-token") -> None:
        self._token = token

    def has_token(self) -> bool:
        return self._token is not None

    def load_access_token(self) -> str:
        if self._token is None:
            raise UpstoxAuthRequiredError("Upstox login is required")
        return self._token


class _FakeTrackedStore:
    def __init__(self, keys: list[str]) -> None:
        self._keys = keys

    def load(self) -> list[str]:
        return self._keys


class _FakeMainScreen:
    def __init__(self, resolved: dict[str, tuple[str, Optional[str]]]) -> None:
        self._resolved = resolved
        self.calls: list[str] = []

    async def resolve_underlying_symbol_and_expiry(
        self, access_token: str, underlying_key: str,
    ) -> tuple[str, Optional[str]]:
        self.calls.append(underlying_key)
        if underlying_key not in self._resolved:
            raise UpstoxApiError("no contracts")
        return self._resolved[underlying_key]


class _FakeSignalsService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def get_signals(
        self,
        access_token: str,
        *,
        underlying_key: str,
        expiry_date: Optional[str] = None,
        underlying_symbol: Optional[str] = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "underlying_key": underlying_key,
                "expiry_date": expiry_date,
                "underlying_symbol": underlying_symbol,
            },
        )
        return {}


@pytest.fixture(autouse=True)
def _market_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults every test to "market is open" -- individual tests override via monkeypatch."""
    monkeypatch.setattr(poller, "is_market_open", lambda: True)


def test_skips_everything_when_market_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(poller, "is_market_open", lambda: False)
    signals = _FakeSignalsService()
    main_screen = _FakeMainScreen({"NSE_INDEX|Nifty 50": ("NIFTY", "2026-07-23")})

    anyio.run(
        lambda: poller._poll_due_instruments(
            token_store=_FakeTokenStore(),
            tracked_store=_FakeTrackedStore(["NSE_INDEX|Nifty 50"]),
            main_screen=main_screen,
            signals_service=signals,
            last_polled={},
        ),
    )

    assert signals.calls == []
    assert main_screen.calls == []


def test_skips_everything_when_no_token_is_stored() -> None:
    signals = _FakeSignalsService()
    main_screen = _FakeMainScreen({"NSE_INDEX|Nifty 50": ("NIFTY", "2026-07-23")})

    anyio.run(
        lambda: poller._poll_due_instruments(
            token_store=_FakeTokenStore(token=None),
            tracked_store=_FakeTrackedStore(["NSE_INDEX|Nifty 50"]),
            main_screen=main_screen,
            signals_service=signals,
            last_polled={},
        ),
    )

    assert signals.calls == []


def test_polls_every_tracked_instrument_with_its_resolved_symbol_and_expiry() -> None:
    signals = _FakeSignalsService()
    main_screen = _FakeMainScreen(
        {
            "NSE_INDEX|Nifty 50": ("NIFTY", "2026-07-23"),
            "NSE_INDEX|Nifty Bank": ("BANKNIFTY", "2026-07-30"),
        },
    )
    last_polled: dict[str, float] = {}

    anyio.run(
        lambda: poller._poll_due_instruments(
            token_store=_FakeTokenStore(),
            tracked_store=_FakeTrackedStore(["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"]),
            main_screen=main_screen,
            signals_service=signals,
            last_polled=last_polled,
        ),
    )

    assert {c["underlying_key"] for c in signals.calls} == {"NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"}
    nifty_call = next(c for c in signals.calls if c["underlying_key"] == "NSE_INDEX|Nifty 50")
    assert nifty_call == {
        "underlying_key": "NSE_INDEX|Nifty 50",
        "expiry_date": "2026-07-23",
        "underlying_symbol": "NIFTY",
    }
    # Both keys are now recorded as just-polled, so a second call this same "instant" is a no-op.
    assert set(last_polled) == {"NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"}


def test_skips_an_instrument_polled_within_the_last_five_minutes() -> None:
    signals = _FakeSignalsService()
    main_screen = _FakeMainScreen({"NSE_INDEX|Nifty 50": ("NIFTY", "2026-07-23")})
    # Recorded "just now" (monotonic() ~ a large positive number) -- well within the 5-minute
    # cooldown for any test's real now(), so this key must be skipped.
    from time import monotonic

    last_polled = {"NSE_INDEX|Nifty 50": monotonic()}

    anyio.run(
        lambda: poller._poll_due_instruments(
            token_store=_FakeTokenStore(),
            tracked_store=_FakeTrackedStore(["NSE_INDEX|Nifty 50"]),
            main_screen=main_screen,
            signals_service=signals,
            last_polled=last_polled,
        ),
    )

    assert signals.calls == []


def test_polls_an_instrument_again_once_five_minutes_have_passed() -> None:
    signals = _FakeSignalsService()
    main_screen = _FakeMainScreen({"NSE_INDEX|Nifty 50": ("NIFTY", "2026-07-23")})
    from time import monotonic

    last_polled = {"NSE_INDEX|Nifty 50": monotonic() - 301.0}

    anyio.run(
        lambda: poller._poll_due_instruments(
            token_store=_FakeTokenStore(),
            tracked_store=_FakeTrackedStore(["NSE_INDEX|Nifty 50"]),
            main_screen=main_screen,
            signals_service=signals,
            last_polled=last_polled,
        ),
    )

    assert len(signals.calls) == 1


def test_one_instrument_failing_does_not_stop_the_others() -> None:
    signals = _FakeSignalsService()
    main_screen = _FakeMainScreen({"NSE_INDEX|Nifty Bank": ("BANKNIFTY", "2026-07-30")})  # Nifty 50 missing -> raises

    anyio.run(
        lambda: poller._poll_due_instruments(
            token_store=_FakeTokenStore(),
            tracked_store=_FakeTrackedStore(["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"]),
            main_screen=main_screen,
            signals_service=signals,
            last_polled={},
        ),
    )

    assert [c["underlying_key"] for c in signals.calls] == ["NSE_INDEX|Nifty Bank"]
