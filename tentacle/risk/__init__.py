"""Risk package: the survival layer that stands OUTSIDE the model.

TENTACLE keeps its OWN internal capital ledger (the rung). The broker balance is
NOT the bot's capital. AccountState is derived from storage (the equity ledger,
closed/open trades, and the current rung) using marketdata for unrealized P&L. The
broker is only an execution and price venue.

This package owns every HARD gate. The brain proposes, the risk layer disposes:
``RiskManager.evaluate`` is the LAW and the model can never override it. Anything
that is unknown (missing price, regime, market-open state) FAILS SAFE, meaning it
BLOCKS a fire rather than letting one slip through by accident.
"""
from __future__ import annotations

from .caps import (
    daily_loss_breached,
    per_trade_stop_price,
    per_trade_target_price,
    ruin_breached,
)
from .cash import available_settled_cash, settle_due, would_cause_gfv
from .manager import RiskManager
from .regime import regime_is_up
from .sizing import position_size

__all__ = [
    "regime_is_up",
    "per_trade_stop_price",
    "per_trade_target_price",
    "daily_loss_breached",
    "ruin_breached",
    "available_settled_cash",
    "would_cause_gfv",
    "settle_due",
    "position_size",
    "RiskManager",
]
