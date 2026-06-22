"""Cash-account discipline: T+1 settlement and Good-Faith-Violation (GFV) avoidance.

In a cash account, proceeds from a sale do not settle until T+1. Buying with cash
that has not yet settled, then selling that new position before the original
proceeds settle, triggers a Good Faith Violation. To stay clean we only ever deploy
SETTLED cash, and we flag any buy that would have to draw on unsettled proceeds.

For a margin account (cfg.risk.cash_account == False) none of this applies and the
GFV check is a no-op.
"""
from __future__ import annotations

from typing import Optional

from ..models import AccountState
from ..util import now_utc


def available_settled_cash(account: AccountState) -> float:
    """Cash that is free to redeploy right now without risking a GFV.

    Prefers the explicit settled_cash field. Never returns a negative number.
    Unreadable inputs -> 0.0 (FAIL SAFE: deploy nothing).
    """
    try:
        settled = float(account.settled_cash)
    except (TypeError, ValueError, AttributeError):
        return 0.0
    if settled != settled:  # NaN guard
        return 0.0
    return max(0.0, settled)


def would_cause_gfv(amount: float, account: AccountState, cfg=None) -> bool:
    """True if buying for ``amount`` would have to draw on unsettled proceeds.

    Only meaningful for a cash account (cfg.risk.cash_account). For a margin account
    this always returns False. FAIL SAFE: unreadable inputs return True (block).
    """
    if cfg is None:
        from ..config import get_config
        cfg = get_config()
    try:
        cash_account = bool(cfg.risk.cash_account)
    except Exception:
        cash_account = True
    if not cash_account:
        return False
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return True
    if amount <= 0:
        return False
    settled = available_settled_cash(account)
    # If the buy exceeds settled cash, the remainder must come from unsettled
    # proceeds, which risks a GFV.
    return amount > settled + 1e-9


def settle_due(storage, cfg=None) -> float:
    """Mark and total any settlements that have come due as of today.

    Thin wrapper over storage.settle_due using today's date (UTC). Returns the total
    dollar amount newly settled. Never raises; 0.0 on any error.
    """
    try:
        today_iso = now_utc().date().isoformat()
    except Exception:
        return 0.0
    try:
        return float(storage.settle_due(today_iso))
    except Exception:
        return 0.0
