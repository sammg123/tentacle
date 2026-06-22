"""The graduation ladder: mastery scoring and rung promotion / demotion / death.

Capital is EARNED rung by rung. The bot starts at rung -1 ($0, backtest) and only
advances when its Mastery score and qualifying-sample count clear the bar for the
current rung. A ruin breach (DEATH) demotes one rung and forces re-qualification,
it never lets the bot silently self-restart at the same size.
"""
from __future__ import annotations

from .mastery import compute_mastery
from .promotion import evaluate_ladder

__all__ = ["compute_mastery", "evaluate_ladder"]
