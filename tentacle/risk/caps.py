"""Pure risk-cap helpers: stop, target, daily-loss halt, and the ruin floor.

These are deliberately tiny and dependency-free. They encode the HARD survival
limits from config.RiskConfig that the model can never cross. They never touch the
network and never raise: on any oddity they fall back to a safe value (a stop at the
entry, a daily-loss / ruin breach reported as True when the inputs are nonsensical).
"""
from __future__ import annotations

from typing import Optional

from ..models import AccountState


def _risk_cfg(cfg=None):
    if cfg is None:
        from ..config import get_config
        cfg = get_config()
    return cfg.risk


def per_trade_stop_price(entry: float, cfg=None) -> float:
    """Hard stop price for a long: entry * (1 - stop_pct).

    Unknown or non-positive entry -> 0.0 (no meaningful stop; sizing will block).
    """
    try:
        entry = float(entry)
    except (TypeError, ValueError):
        return 0.0
    if entry <= 0:
        return 0.0
    try:
        stop_pct = float(_risk_cfg(cfg).per_trade_stop_pct)
    except Exception:
        stop_pct = 0.08
    return entry * (1.0 - stop_pct)


def per_trade_target_price(entry: float, cfg=None) -> float:
    """Profit-take target for a long: entry * (1 + target_pct).

    Unknown or non-positive entry -> 0.0.
    """
    try:
        entry = float(entry)
    except (TypeError, ValueError):
        return 0.0
    if entry <= 0:
        return 0.0
    try:
        target_pct = float(_risk_cfg(cfg).per_trade_target_pct)
    except Exception:
        target_pct = 0.15
    return entry * (1.0 + target_pct)


def daily_loss_breached(account: AccountState, cfg=None) -> bool:
    """True when realized losses today have hit the daily-loss halt threshold.

    Breach when realized_pnl_today <= -(daily_loss_halt_pct * rung_capital). FAIL
    SAFE: if the inputs cannot be read we report a breach (halt) rather than allow
    more trading on an unknown state.
    """
    try:
        realized = float(account.realized_pnl_today)
        rung_capital = float(account.rung_capital)
    except (TypeError, ValueError, AttributeError):
        return True
    if rung_capital <= 0:
        # No real capital at this rung means nothing to lose; do not halt purely
        # for a zero-capital (shadow/paper rung). Only halt when there is capital.
        return False
    try:
        halt_pct = float(_risk_cfg(cfg).daily_loss_halt_pct)
    except Exception:
        halt_pct = 0.10
    return realized <= -(halt_pct * rung_capital)


def ruin_breached(equity: float, stage_start: float, cfg=None) -> bool:
    """True when stage equity has fallen to the ruin floor: DEATH.

    Breach when equity <= stage_start * (1 - ruin_floor_pct). FAIL SAFE: unreadable
    inputs report a breach.
    """
    try:
        equity = float(equity)
        stage_start = float(stage_start)
    except (TypeError, ValueError):
        return True
    if stage_start <= 0:
        # A zero-capital stage cannot breach a percentage ruin floor.
        return False
    try:
        floor_pct = float(_risk_cfg(cfg).ruin_floor_pct)
    except Exception:
        floor_pct = 0.40
    return equity <= stage_start * (1.0 - floor_pct)
