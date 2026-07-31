from __future__ import annotations

from app.services import quantity_sizing as qs


def test_max_affordable_quantity_hand_computed():
    # allotted = 100000 * 20% = 20000; minus buffer 1000, charges 200 = 18800
    # cost/lot = 100 * 75 = 7500; affordable lots = floor(18800/7500) = 2 -> 150
    result = qs.compute_max_affordable_quantity(
        use_dynamic_lot_selection=True,
        available_capital=100000,
        capital_allocation_percent=20,
        buffer_amount=1000,
        estimated_charges=200,
        entry_price=100,
        lot_size=75,
    )
    assert result == 150


def test_max_affordable_quantity_none_when_disabled():
    assert qs.compute_max_affordable_quantity(
        use_dynamic_lot_selection=False,
        available_capital=100000,
        capital_allocation_percent=20,
        buffer_amount=0,
        estimated_charges=None,
        entry_price=100,
        lot_size=75,
    ) is None


def test_max_affordable_quantity_none_without_entry_price():
    assert qs.compute_max_affordable_quantity(
        use_dynamic_lot_selection=True,
        available_capital=100000,
        capital_allocation_percent=20,
        buffer_amount=0,
        estimated_charges=None,
        entry_price=None,
        lot_size=75,
    ) is None


def test_risk_based_quantity_hand_computed():
    # sl_distance = 100 * 5% = 5; perLotRisk = 5 * 75 = 375
    # riskLots = floor(3000 / 375) = 8 lots -> 600 units, not capped by capital (huge capital)
    result = qs.compute_risk_based_quantity(
        risk_per_trade_amount=3000,
        risk_management_is_percent=True,
        stop_loss_value=5,
        entry_price=100,
        lot_size=75,
        available_capital=10_000_000,
        capital_allocation_percent=100,
        buffer_amount=0,
        estimated_charges=None,
    )
    assert result is not None
    assert result.quantity == 600
    assert result.capped_by_capital is False
    assert result.risk_budget_too_small is False


def test_risk_based_quantity_capped_by_capital():
    # riskLots would be huge, but capital only affords 1 lot (entry price high, tiny capital)
    result = qs.compute_risk_based_quantity(
        risk_per_trade_amount=1_000_000,
        risk_management_is_percent=True,
        stop_loss_value=5,
        entry_price=1000,
        lot_size=75,
        available_capital=100_000,
        capital_allocation_percent=100,
        buffer_amount=0,
        estimated_charges=None,
    )
    assert result is not None
    # cost/lot = 1000*75 = 75000; affordable lots = floor(100000/75000) = 1 -> 75
    assert result.quantity == 75
    assert result.capped_by_capital is True


def test_risk_based_quantity_risk_budget_too_small_still_returns_one_lot():
    # perLotRisk huge relative to risk_per_trade_amount -> riskBasedLots = 0 -> floored to 1
    result = qs.compute_risk_based_quantity(
        risk_per_trade_amount=10,
        risk_management_is_percent=True,
        stop_loss_value=5,
        entry_price=100,
        lot_size=75,
        available_capital=10_000_000,
        capital_allocation_percent=100,
        buffer_amount=0,
        estimated_charges=None,
    )
    assert result is not None
    assert result.quantity == 75
    assert result.risk_budget_too_small is True


def test_risk_based_quantity_none_without_stop_loss_distance():
    assert qs.compute_risk_based_quantity(
        risk_per_trade_amount=3000,
        risk_management_is_percent=True,
        stop_loss_value=0,
        entry_price=100,
        lot_size=75,
        available_capital=100000,
        capital_allocation_percent=20,
        buffer_amount=0,
        estimated_charges=None,
    ) is None


def test_atr_based_quantity_hand_computed():
    # sl_distance = atr(10) * |delta(0.5)| * multiplier(1.5) = 7.5; perLotRisk = 7.5*75=562.5
    # riskLots = floor(3000/562.5) = 5 -> 375
    result = qs.compute_atr_based_quantity(
        risk_per_trade_amount=3000,
        atr_14_5m=10,
        contract_delta=-0.5,
        atr_stop_multiplier=1.5,
        entry_price=100,
        lot_size=75,
        available_capital=10_000_000,
        capital_allocation_percent=100,
        buffer_amount=0,
        estimated_charges=None,
    )
    assert result is not None
    assert result.quantity == 375


def test_atr_based_quantity_none_without_atr_or_delta():
    assert qs.compute_atr_based_quantity(
        risk_per_trade_amount=3000, atr_14_5m=None, contract_delta=0.5,
        atr_stop_multiplier=1.5, entry_price=100, lot_size=75,
        available_capital=100000, capital_allocation_percent=20, buffer_amount=0,
        estimated_charges=None,
    ) is None
    assert qs.compute_atr_based_quantity(
        risk_per_trade_amount=3000, atr_14_5m=10, contract_delta=None,
        atr_stop_multiplier=1.5, entry_price=100, lot_size=75,
        available_capital=100000, capital_allocation_percent=20, buffer_amount=0,
        estimated_charges=None,
    ) is None


def test_iv_based_quantity_hand_computed():
    import math
    entry = 100.0
    iv = 25.0
    multiplier = 1.0
    sl_distance = entry * (iv / 100.0) * math.sqrt(1.0 / 365.0) * multiplier
    per_lot_risk = sl_distance * 75
    expected_lots = int(3000 / per_lot_risk)
    result = qs.compute_iv_based_quantity(
        risk_per_trade_amount=3000,
        contract_iv=iv,
        iv_stop_multiplier=multiplier,
        entry_price=entry,
        lot_size=75,
        available_capital=10_000_000,
        capital_allocation_percent=100,
        buffer_amount=0,
        estimated_charges=None,
    )
    assert result is not None
    assert result.quantity == max(expected_lots, 1) * 75


def test_kelly_based_quantity_hand_computed():
    # W=0.6, R=800/300, rawKelly = 0.6 - 0.4/R, half=rawKelly*0.5 (within cap 0.25)
    # kellyRiskAmount = kellyFraction * 100000
    # sl_distance = 100*5% = 5; perLotRisk = 5*75=375; riskLots = floor(kellyRiskAmount/375)
    # NOTE: the exact-decimal math gives 60 lots (4500), but IEEE-754 float arithmetic (identical
    # in Kotlin and Python -- both use doubles) makes kellyRiskAmount land at 22499.999999999996,
    # not exactly 22500, so int(.../375) truncates to 59, not 60. This is a real floating-point
    # property of the formula itself, not a porting bug -- the Kotlin source has the identical
    # behavior for the identical inputs, which is exactly the "numeric parity" this port is for.
    win_rate = 0.6
    ratio = 800.0 / 300.0
    raw_kelly = win_rate - (1 - win_rate) / ratio
    kelly_fraction = min(raw_kelly * 0.5, 0.25)
    kelly_risk_amount = kelly_fraction * 100000
    expected_lots = int(kelly_risk_amount / 375)
    result = qs.compute_kelly_based_quantity(
        trade_count=40,
        win_rate=60.0,
        average_win=800.0,
        average_loss=-300.0,
        capital_for_kelly=100000,
        risk_management_is_percent=True,
        stop_loss_value=5,
        entry_price=100,
        lot_size=75,
        available_capital=10_000_000,
        capital_allocation_percent=100,
        buffer_amount=0,
        estimated_charges=None,
    )
    assert result is not None
    assert result.quantity == max(expected_lots, 1) * 75


def test_kelly_based_quantity_caps_fraction_at_quarter_capital():
    # Extremely favorable stats -> rawKelly*0.5 would exceed 0.25, must clamp.
    # W=0.9, R=10 -> rawKelly = 0.9 - 0.1/10 = 0.89, half=0.445 -> clamp to 0.25
    # kellyRiskAmount = 0.25 * 100000 = 25000; perLotRisk=375; lots=floor(25000/375)=66 -> 4950
    result = qs.compute_kelly_based_quantity(
        trade_count=50, win_rate=90.0, average_win=1000.0, average_loss=-100.0,
        capital_for_kelly=100000, risk_management_is_percent=True, stop_loss_value=5,
        entry_price=100, lot_size=75, available_capital=10_000_000,
        capital_allocation_percent=100, buffer_amount=0, estimated_charges=None,
    )
    assert result is not None
    assert result.quantity == 4950


def test_kelly_based_quantity_none_without_trades():
    assert qs.compute_kelly_based_quantity(
        trade_count=0, win_rate=60.0, average_win=800.0, average_loss=-300.0,
        capital_for_kelly=100000, risk_management_is_percent=True, stop_loss_value=5,
        entry_price=100, lot_size=75, available_capital=100000,
        capital_allocation_percent=20, buffer_amount=0, estimated_charges=None,
    ) is None


def test_default_quantity_returns_held_quantity_when_position_open():
    # Any mode -- held_quantity > 0 always short-circuits (opposite-tap closes the position).
    assert qs.default_quantity(
        held_quantity=150, mode="RISK_BASED", available_capital=None,
        capital_allocation_percent=0, buffer_amount=0, estimated_charges=None,
        entry_price=None, lot_size=75, default_lot_count=1,
    ) == 150


def test_default_quantity_fixed_mode_uses_default_lot_count():
    assert qs.default_quantity(
        held_quantity=0, mode="FIXED", available_capital=None,
        capital_allocation_percent=0, buffer_amount=0, estimated_charges=None,
        entry_price=None, lot_size=75, default_lot_count=3,
    ) == 225


def test_default_quantity_falls_back_to_one_lot_when_inputs_missing():
    # RISK_BASED with no entry_price -> compute_risk_based_quantity returns None -> 1 lot fallback
    assert qs.default_quantity(
        held_quantity=0, mode="RISK_BASED", available_capital=None,
        capital_allocation_percent=0, buffer_amount=0, estimated_charges=None,
        entry_price=None, lot_size=75, default_lot_count=1,
    ) == 75


def test_default_quantity_capital_based_falls_back_to_one_lot_when_zero():
    result = qs.default_quantity(
        held_quantity=0, mode="CAPITAL_BASED", available_capital=100,
        capital_allocation_percent=1, buffer_amount=0, estimated_charges=None,
        entry_price=1000, lot_size=75, default_lot_count=1,
    )
    # allotted = 100*1% = 1, cost/lot = 75000 -> affordable = 0 -> falsy -> fallback to 1 lot (75)
    assert result == 75
