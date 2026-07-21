from __future__ import annotations

from app.services.main_screen_service import _entry_price


def test_uses_average_price_when_present_and_nonzero() -> None:
    position = {"quantity": 520, "average_price": 107.7, "buy_price": 999.0}

    assert _entry_price(position) == 107.7


def test_falls_back_to_buy_price_for_a_long_position_when_average_price_is_null() -> None:
    """Upstox documents average_price as null for a position with no prior-session holdings --
    a pure same-day position, the normal case for this app -- not just a momentary post-fill lag."""
    position = {"quantity": 520, "average_price": None, "buy_price": 107.7, "sell_price": 0.0}

    assert _entry_price(position) == 107.7


def test_falls_back_to_sell_price_for_a_short_position_when_average_price_is_zero() -> None:
    position = {"quantity": -520, "average_price": 0.0, "buy_price": 0.0, "sell_price": 92.4}

    assert _entry_price(position) == 92.4


def test_returns_zero_when_average_price_and_the_directional_fallback_are_both_missing() -> None:
    position = {"quantity": 520, "average_price": None, "buy_price": None}

    assert _entry_price(position) == 0.0


def test_returns_zero_for_a_flat_position() -> None:
    position = {"quantity": 0, "average_price": None, "buy_price": 107.7}

    assert _entry_price(position) == 0.0


def test_returns_zero_when_quantity_is_missing() -> None:
    position = {"average_price": None, "buy_price": 107.7}

    assert _entry_price(position) == 0.0
