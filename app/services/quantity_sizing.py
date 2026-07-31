from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Optional

# Direct port of QuantitySizing.kt -- pure, stateless math, no repository/network calls, same
# "pure function" shape as GexCalculator/OiCalculator/OrderFlowAnalyzer's own ports. Moved
# server-side per WEB_CLIENT_ROADMAP.md's M3 entry: "sizing errors are money bugs," so this is a
# faithful 1:1 port (all six modes), not a narrowed one, mirrored with the same hand-computed-
# value test rigor the other ports already established.

Mode = Literal["FIXED", "CAPITAL_BASED", "RISK_BASED", "ATR_BASED", "IV_BASED", "KELLY"]

# Half-Kelly's own hard ceiling -- however good the account's track record looks, never treat
# more than this fraction of capital as the risk budget for a single trade.
_KELLY_MAX_CAPITAL_FRACTION = 0.25


@dataclass(frozen=True)
class RiskBasedQuantityResult:
    quantity: int
    # True when the capital allocation cap reduced the quantity below what the risk budget alone
    # would have allowed.
    capped_by_capital: bool
    # True when even a single lot's risk already exceeds the input risk budget -- quantity still
    # comes out to one lot (a stepper needs *some* starting value), but the actual risk on that
    # one lot is higher than requested.
    risk_budget_too_small: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "quantity": self.quantity,
            "capped_by_capital": self.capped_by_capital,
            "risk_budget_too_small": self.risk_budget_too_small,
        }


def compute_max_affordable_quantity(
    *,
    use_dynamic_lot_selection: bool,
    available_capital: Optional[float],
    capital_allocation_percent: float,
    buffer_amount: float,
    estimated_charges: Optional[float],
    entry_price: Optional[float],
    lot_size: int,
) -> Optional[int]:
    """The most units of a contract the configured capital allocation can afford right now,
    already rounded down to a whole multiple of lot_size -- or None when
    use_dynamic_lot_selection is off, or there isn't yet enough data (no available_capital, or no
    usable entry_price)."""
    if not use_dynamic_lot_selection:
        return None
    safe_lot_size = max(lot_size, 1)
    if entry_price is None or entry_price <= 0.0:
        return None
    if available_capital is None:
        return None

    allotted_before_reserves = available_capital * capital_allocation_percent / 100.0
    allotted_capital = max(allotted_before_reserves - buffer_amount - (estimated_charges or 0.0), 0.0)
    cost_per_lot = entry_price * safe_lot_size
    if cost_per_lot <= 0.0:
        return None

    affordable_lots = int(allotted_capital / cost_per_lot)
    return affordable_lots * safe_lot_size


def _size_by_per_lot_risk(
    *,
    risk_per_trade_amount: float,
    per_lot_risk: float,
    lot_size: int,
    entry_price: float,
    available_capital: Optional[float],
    capital_allocation_percent: float,
    buffer_amount: float,
    estimated_charges: Optional[float],
) -> RiskBasedQuantityResult:
    """Shared tail of every risk-style sizing function: risk_per_trade_amount rupees divided by
    per_lot_risk, floored to a whole lot count, capped by compute_max_affordable_quantity's
    capital-allocation limit -- a risk budget alone doesn't guarantee the capital to actually
    afford that many lots."""
    risk_based_lots = int(risk_per_trade_amount / per_lot_risk) if risk_per_trade_amount > 0.0 else 0
    risk_budget_too_small = risk_based_lots < 1

    capital_cap_quantity = compute_max_affordable_quantity(
        use_dynamic_lot_selection=True,
        available_capital=available_capital,
        capital_allocation_percent=capital_allocation_percent,
        buffer_amount=buffer_amount,
        estimated_charges=estimated_charges,
        entry_price=entry_price,
        lot_size=lot_size,
    )
    capital_cap_lots = capital_cap_quantity // lot_size if capital_cap_quantity is not None else None

    risk_lots = max(risk_based_lots, 1)
    final_lots = min(risk_lots, max(capital_cap_lots, 0)) if capital_cap_lots is not None else risk_lots
    capped_by_capital = capital_cap_lots is not None and capital_cap_lots < risk_lots

    return RiskBasedQuantityResult(
        quantity=max(final_lots, 1) * lot_size,
        capped_by_capital=capped_by_capital,
        risk_budget_too_small=risk_budget_too_small,
    )


def compute_risk_based_quantity(
    *,
    risk_per_trade_amount: float,
    risk_management_is_percent: bool,
    stop_loss_value: float,
    entry_price: Optional[float],
    lot_size: int,
    available_capital: Optional[float],
    capital_allocation_percent: float,
    buffer_amount: float,
    estimated_charges: Optional[float],
) -> Optional[RiskBasedQuantityResult]:
    """As many units as risk_per_trade_amount rupees of risk affords, given the configured
    stop-loss distance -- perLotRisk is stop_loss_value percent of entry_price (when
    risk_management_is_percent) or a flat rupee/points offset, times lot_size."""
    safe_lot_size = max(lot_size, 1)
    if entry_price is None or entry_price <= 0.0:
        return None
    sl_distance = entry_price * stop_loss_value / 100.0 if risk_management_is_percent else stop_loss_value
    if sl_distance <= 0.0:
        return None

    return _size_by_per_lot_risk(
        risk_per_trade_amount=risk_per_trade_amount,
        per_lot_risk=sl_distance * safe_lot_size,
        lot_size=safe_lot_size,
        entry_price=entry_price,
        available_capital=available_capital,
        capital_allocation_percent=capital_allocation_percent,
        buffer_amount=buffer_amount,
        estimated_charges=estimated_charges,
    )


def compute_atr_based_quantity(
    *,
    risk_per_trade_amount: float,
    atr_14_5m: Optional[float],
    contract_delta: Optional[float],
    atr_stop_multiplier: float,
    entry_price: Optional[float],
    lot_size: int,
    available_capital: Optional[float],
    capital_allocation_percent: float,
    buffer_amount: float,
    estimated_charges: Optional[float],
) -> Optional[RiskBasedQuantityResult]:
    """Stop distance = ATR14(5m) * |delta| * atr_stop_multiplier, then the same risk-sizing tail
    as compute_risk_based_quantity."""
    safe_lot_size = max(lot_size, 1)
    if entry_price is None or entry_price <= 0.0:
        return None
    if atr_14_5m is None or atr_14_5m <= 0.0:
        return None
    if contract_delta is None:
        return None
    delta = abs(contract_delta)
    if delta <= 0.0:
        return None
    sl_distance = atr_14_5m * delta * atr_stop_multiplier
    if sl_distance <= 0.0:
        return None

    return _size_by_per_lot_risk(
        risk_per_trade_amount=risk_per_trade_amount,
        per_lot_risk=sl_distance * safe_lot_size,
        lot_size=safe_lot_size,
        entry_price=entry_price,
        available_capital=available_capital,
        capital_allocation_percent=capital_allocation_percent,
        buffer_amount=buffer_amount,
        estimated_charges=estimated_charges,
    )


def compute_iv_based_quantity(
    *,
    risk_per_trade_amount: float,
    contract_iv: Optional[float],
    iv_stop_multiplier: float,
    entry_price: Optional[float],
    lot_size: int,
    available_capital: Optional[float],
    capital_allocation_percent: float,
    buffer_amount: float,
    estimated_charges: Optional[float],
) -> Optional[RiskBasedQuantityResult]:
    """Stop distance = entry_price * (iv/100) * sqrt(1/365) * iv_stop_multiplier (the standard
    expected-1-day-move approximation), then the same risk-sizing tail."""
    safe_lot_size = max(lot_size, 1)
    if entry_price is None or entry_price <= 0.0:
        return None
    if contract_iv is None or contract_iv <= 0.0:
        return None
    sl_distance = entry_price * (contract_iv / 100.0) * math.sqrt(1.0 / 365.0) * iv_stop_multiplier
    if sl_distance <= 0.0:
        return None

    return _size_by_per_lot_risk(
        risk_per_trade_amount=risk_per_trade_amount,
        per_lot_risk=sl_distance * safe_lot_size,
        lot_size=safe_lot_size,
        entry_price=entry_price,
        available_capital=available_capital,
        capital_allocation_percent=capital_allocation_percent,
        buffer_amount=buffer_amount,
        estimated_charges=estimated_charges,
    )


def compute_kelly_based_quantity(
    *,
    trade_count: int,
    win_rate: Optional[float],
    average_win: Optional[float],
    average_loss: Optional[float],
    capital_for_kelly: Optional[float],
    risk_management_is_percent: bool,
    stop_loss_value: float,
    entry_price: Optional[float],
    lot_size: int,
    available_capital: Optional[float],
    capital_allocation_percent: float,
    buffer_amount: float,
    estimated_charges: Optional[float],
) -> Optional[RiskBasedQuantityResult]:
    """Half-Kelly fraction of capital_for_kelly, from win_rate/average_win/average_loss:
    W = win_rate/100, R = average_win / abs(average_loss), rawKelly = W - (1-W)/R,
    fraction = clamp(rawKelly * 0.5, 0, 0.25). Then routes through compute_risk_based_quantity
    with that fraction's rupee amount as the risk budget."""
    if trade_count <= 0:
        return None
    if capital_for_kelly is None or capital_for_kelly <= 0.0:
        return None
    if average_loss is None or average_loss == 0.0:
        return None
    avg_loss_abs = abs(average_loss)
    if win_rate is None:
        return None
    win_rate_fraction = min(max(win_rate / 100.0, 0.0), 1.0)
    win_loss_ratio = (average_win or 0.0) / avg_loss_abs
    raw_kelly = win_rate_fraction - (1.0 - win_rate_fraction) / win_loss_ratio
    kelly_fraction = min(max(raw_kelly * 0.5, 0.0), _KELLY_MAX_CAPITAL_FRACTION)
    kelly_risk_amount = kelly_fraction * capital_for_kelly

    return compute_risk_based_quantity(
        risk_per_trade_amount=kelly_risk_amount,
        risk_management_is_percent=risk_management_is_percent,
        stop_loss_value=stop_loss_value,
        entry_price=entry_price,
        lot_size=lot_size,
        available_capital=available_capital,
        capital_allocation_percent=capital_allocation_percent,
        buffer_amount=buffer_amount,
        estimated_charges=estimated_charges,
    )


def default_quantity(
    *,
    held_quantity: int,
    mode: Mode,
    available_capital: Optional[float],
    capital_allocation_percent: float,
    buffer_amount: float,
    estimated_charges: Optional[float],
    entry_price: Optional[float],
    lot_size: int,
    default_lot_count: int,
    risk_per_trade_amount: float = 0.0,
    risk_management_is_percent: bool = True,
    stop_loss_value: float = 0.0,
    atr_14_5m: Optional[float] = None,
    contract_delta: Optional[float] = None,
    contract_iv: Optional[float] = None,
    atr_stop_multiplier: float = 1.5,
    iv_stop_multiplier: float = 1.0,
    kelly_trade_count: int = 0,
    kelly_win_rate: Optional[float] = None,
    kelly_average_win: Optional[float] = None,
    kelly_average_loss: Optional[float] = None,
    kelly_capital: Optional[float] = None,
) -> int:
    """A trading panel's lots-stepper starting quantity. If held_quantity is positive (an open
    position already exists for this exact instrument), returns it unchanged -- so tapping the
    opposite-side action actually flattens it instead of some unrelated capital-sized quantity
    that could overshoot and flip a close into a fresh position on the other side. Otherwise
    dispatches per mode, each falling back to one lot if its own inputs aren't available yet."""
    if held_quantity > 0:
        return held_quantity
    safe_lot_size = max(lot_size, 1)

    if mode == "CAPITAL_BASED":
        quantity = compute_max_affordable_quantity(
            use_dynamic_lot_selection=True,
            available_capital=available_capital,
            capital_allocation_percent=capital_allocation_percent,
            buffer_amount=buffer_amount,
            estimated_charges=estimated_charges,
            entry_price=entry_price,
            lot_size=safe_lot_size,
        )
        return quantity if quantity else safe_lot_size

    if mode == "RISK_BASED":
        result = compute_risk_based_quantity(
            risk_per_trade_amount=risk_per_trade_amount,
            risk_management_is_percent=risk_management_is_percent,
            stop_loss_value=stop_loss_value,
            entry_price=entry_price,
            lot_size=safe_lot_size,
            available_capital=available_capital,
            capital_allocation_percent=capital_allocation_percent,
            buffer_amount=buffer_amount,
            estimated_charges=estimated_charges,
        )
        return result.quantity if result and result.quantity > 0 else safe_lot_size

    if mode == "ATR_BASED":
        result = compute_atr_based_quantity(
            risk_per_trade_amount=risk_per_trade_amount,
            atr_14_5m=atr_14_5m,
            contract_delta=contract_delta,
            atr_stop_multiplier=atr_stop_multiplier,
            entry_price=entry_price,
            lot_size=safe_lot_size,
            available_capital=available_capital,
            capital_allocation_percent=capital_allocation_percent,
            buffer_amount=buffer_amount,
            estimated_charges=estimated_charges,
        )
        return result.quantity if result and result.quantity > 0 else safe_lot_size

    if mode == "IV_BASED":
        result = compute_iv_based_quantity(
            risk_per_trade_amount=risk_per_trade_amount,
            contract_iv=contract_iv,
            iv_stop_multiplier=iv_stop_multiplier,
            entry_price=entry_price,
            lot_size=safe_lot_size,
            available_capital=available_capital,
            capital_allocation_percent=capital_allocation_percent,
            buffer_amount=buffer_amount,
            estimated_charges=estimated_charges,
        )
        return result.quantity if result and result.quantity > 0 else safe_lot_size

    if mode == "KELLY":
        result = compute_kelly_based_quantity(
            trade_count=kelly_trade_count,
            win_rate=kelly_win_rate,
            average_win=kelly_average_win,
            average_loss=kelly_average_loss,
            capital_for_kelly=kelly_capital,
            risk_management_is_percent=risk_management_is_percent,
            stop_loss_value=stop_loss_value,
            entry_price=entry_price,
            lot_size=safe_lot_size,
            available_capital=available_capital,
            capital_allocation_percent=capital_allocation_percent,
            buffer_amount=buffer_amount,
            estimated_charges=estimated_charges,
        )
        return result.quantity if result and result.quantity > 0 else safe_lot_size

    # FIXED
    return max(default_lot_count, 1) * safe_lot_size
