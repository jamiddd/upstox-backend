from __future__ import annotations

import pytest

from app.services.default_bracket import default_bracket_prices


def test_long_entry_target_above_stoploss_below() -> None:
    target, stoploss = default_bracket_prices(100.0, "BUY")
    assert target == pytest.approx(105.0)
    assert stoploss == pytest.approx(95.0)


def test_short_entry_target_below_stoploss_above() -> None:
    target, stoploss = default_bracket_prices(100.0, "SELL")
    assert target == pytest.approx(95.0)
    assert stoploss == pytest.approx(105.0)


def test_transaction_type_is_case_insensitive() -> None:
    target, stoploss = default_bracket_prices(100.0, "buy")
    assert target == pytest.approx(105.0)
    assert stoploss == pytest.approx(95.0)


def test_custom_percentages() -> None:
    target, stoploss = default_bracket_prices(200.0, "BUY", target_pct=0.1, stoploss_pct=0.02)
    assert target == pytest.approx(220.0)
    assert stoploss == pytest.approx(196.0)
