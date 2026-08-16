from __future__ import annotations

"""Shared realized-P&L formula, factored out of `exit_reconciliation_checker.py` when Part 5
(`docs/ORDER_HISTORY_V2_DESIGN.md`) needed the identical math for its own exit-fill ledger
derivation -- one implementation, so the reconciler and the recorder can't silently drift into
two different versions of the same formula."""


def compute_realized_pnl(
    *, entry_price: float, avg_exit_price: float, filled_quantity: float, transaction_type: str,
) -> float:
    """[transaction_type] is the lot's own *entry* side (`"BUY"` for a long lot, `"SELL"` for a
    short) -- matches the Android client's `LotExitApplier` sign convention exactly: `sign = -1`
    for a SELL-entry/short lot, `+1` for a BUY-entry/long lot."""
    sign = -1.0 if transaction_type.upper() == "SELL" else 1.0
    return (avg_exit_price - entry_price) * filled_quantity * sign
