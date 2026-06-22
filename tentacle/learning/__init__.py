"""The learning loop: outcomes, calibration, and self-tuning.

Every candidate (fired OR shadow) gets a pending Outcome so the bot learns from
the road not taken. When a horizon elapses the outcome is resolved into win, loss,
or flat. Calibration measures overconfidence (predicted win-prob vs realized win
rate). The tuner nudges per-source reliability and per-catalyst priors from the
resolved record, slowly and only with enough samples to be meaningful.
"""
from __future__ import annotations

from .calibration import calibration_error
from .outcomes import log_outcome, resolve_due_outcomes
from .tuner import retune

__all__ = [
    "log_outcome",
    "resolve_due_outcomes",
    "calibration_error",
    "retune",
]
