from __future__ import annotations

"""External-order detection's default bracket (see `_record_order_history_from_push` in
`app/main.py`): an order placed in Upstox's own app carries no client-supplied target/stoploss,
so that fallback path uses this fixed +-5% bracket, applied around whatever price the fill
actually filled at, so the position is never left genuinely unprotected. Pure/testable, same
"pure calculator" split as `order_engine_lot_bracket_armer.arm_lot_bracket`'s own inputs."""

DEFAULT_TARGET_PCT = 0.05
DEFAULT_STOPLOSS_PCT = 0.05


def default_bracket_prices(
    average_price: float,
    transaction_type: str,
    *,
    target_pct: float = DEFAULT_TARGET_PCT,
    stoploss_pct: float = DEFAULT_STOPLOSS_PCT,
) -> tuple[float, float]:
    """Returns `(target_price, stoploss_price)` at [target_pct]/[stoploss_pct] away from
    [average_price], mirrored the same way `arm_lot_bracket`'s own `is_long` check does: a long
    (`BUY`) entry's target sits above and stop-loss below entry price; a short (`SELL`) entry is
    the mirror image."""
    is_long = transaction_type.upper() == "BUY"
    if is_long:
        return average_price * (1 + target_pct), average_price * (1 - stoploss_pct)
    return average_price * (1 - target_pct), average_price * (1 + stoploss_pct)
