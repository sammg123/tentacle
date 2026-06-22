"""Mastery score: the single number that decides whether the bot graduates.

Mastery blends six components (each normalized to 0..1) using config.MasteryWeights,
then scales to 0..100:

  profit_factor  gross wins / gross losses, squashed via x/(x+1) to 0..1
  win_rate       fraction of resolved outcomes that won
  drawdown       1 - maxDD on the stage equity curve (lower DD == higher mastery)
  calibration    1 - calibration_error (overconfidence is punished directly)
  sample_size    saturating n/(n+threshold) so a tiny lucky streak cannot graduate
  expectancy     average R per trade, mapped to 0..1

We use resolved outcomes (fired + shadow) for the statistical components and the
closed trades / equity curve for drawdown. Empty history returns (0.0, {...}) so a
fresh bot has zero mastery and cannot promote by accident.
"""
from __future__ import annotations

from typing import Optional

from ..config import Config, get_config
from ..models import OutcomeLabel
from ..util import get_logger

log = get_logger("tentacle.ladder.mastery")

# Sample-size saturation threshold: n/(n+THRESHOLD). At n==THRESHOLD the component
# is 0.5; it approaches 1.0 only with a large, sustained record.
_SAMPLE_THRESHOLD = 40
# Per-trade stop is the unit of R for expectancy. We read it from cfg.risk so the
# mapping tracks the configured risk per trade.


def compute_mastery(storage, cfg: Optional[Config] = None, rung=None) -> tuple[float, dict]:
    """Return (mastery 0..100, components dict).

    ``rung`` is accepted for interface symmetry (the ladder may pass the current
    rung); the statistics are drawn from the current stage's resolved outcomes and
    closed trades regardless. Empty history -> (0.0, components-all-zero).
    """
    cfg = cfg or get_config()
    weights = cfg.mastery

    components = {
        "profit_factor": 0.0,
        "win_rate": 0.0,
        "drawdown": 0.0,
        "calibration": 0.0,
        "sample_size": 0.0,
        "expectancy": 0.0,
    }

    try:
        outcomes = [
            o for o in storage.resolved_outcomes()
            if o.get("label") != OutcomeLabel.PENDING.value
        ]
    except Exception as exc:  # noqa: BLE001
        log.debug("resolved_outcomes failed: %s", exc)
        outcomes = []

    n = len(outcomes)

    # Calibration is meaningful even with a thin record (returns 0.5 unknown when
    # empty), and it should pull mastery DOWN when unknown, so include it always.
    try:
        from ..learning.calibration import calibration_error
        cal_err = calibration_error(storage, cfg)
    except Exception as exc:  # noqa: BLE001
        log.debug("calibration_error failed: %s", exc)
        cal_err = 0.5
    components["calibration"] = _clamp(1.0 - cal_err, 0.0, 1.0)

    if n == 0:
        # No resolved record: only the (unknown) calibration term has any value, but
        # with zero samples the honest mastery is 0. Return all-zero so we never
        # promote on nothing.
        return 0.0, {k: round(v, 4) for k, v in {**components, "calibration": 0.0}.items()}

    # ----------------------------------------------------- win rate + profit factor
    wins = 0
    gross_win = 0.0
    gross_loss = 0.0
    drifts: list[float] = []
    for o in outcomes:
        label = o.get("label")
        d = o.get("drift_pct")
        try:
            d = float(d) if d is not None else None
        except (TypeError, ValueError):
            d = None
        if d is not None:
            drifts.append(d)
            if d > 0:
                gross_win += d
            elif d < 0:
                gross_loss += -d
        if label == OutcomeLabel.WIN.value:
            wins += 1

    components["win_rate"] = _clamp(wins / n, 0.0, 1.0)

    if gross_loss > 0:
        pf = gross_win / gross_loss
    elif gross_win > 0:
        pf = float("inf")
    else:
        pf = 0.0
    # Squash profit factor to 0..1. x/(x+1): pf=1 -> 0.5, pf=3 -> 0.75, inf -> 1.0.
    if pf == float("inf"):
        components["profit_factor"] = 1.0
    else:
        components["profit_factor"] = _clamp(pf / (pf + 1.0), 0.0, 1.0)

    # ----------------------------------------------------------------- expectancy
    # Average drift per outcome expressed in R (units of the per-trade stop). A
    # positive expectancy of ~0.5R maps to ~0.62 here; flat/negative pulls it down.
    if drifts:
        avg_drift = sum(drifts) / len(drifts)
        stop = max(float(getattr(cfg.risk, "per_trade_stop_pct", 0.08) or 0.08), 1e-6)
        avg_r = avg_drift / stop
        # Logistic-ish map centered at 0R -> 0.5, +1R -> ~0.73, -1R -> ~0.27.
        components["expectancy"] = _clamp(1.0 / (1.0 + _safe_exp(-avg_r)), 0.0, 1.0)
    else:
        components["expectancy"] = 0.0

    # ----------------------------------------------------------------- drawdown
    components["drawdown"] = _clamp(1.0 - _max_drawdown(storage, cfg), 0.0, 1.0)

    # --------------------------------------------------------------- sample size
    components["sample_size"] = _clamp(n / (n + _SAMPLE_THRESHOLD), 0.0, 1.0)

    # ----------------------------------------------------------------- blend
    score01 = (
        weights.profit_factor * components["profit_factor"]
        + weights.win_rate * components["win_rate"]
        + weights.drawdown * components["drawdown"]
        + weights.calibration * components["calibration"]
        + weights.sample_size * components["sample_size"]
        + weights.expectancy * components["expectancy"]
    )
    total_w = (
        weights.profit_factor + weights.win_rate + weights.drawdown
        + weights.calibration + weights.sample_size + weights.expectancy
    )
    if total_w > 0:
        score01 = score01 / total_w
    mastery = _clamp(score01 * 100.0, 0.0, 100.0)

    return mastery, {k: round(v, 4) for k, v in components.items()}


def _max_drawdown(storage, cfg: Config) -> float:
    """Max peak-to-trough drawdown on the stage equity curve, as a fraction 0..1.

    Uses the persisted equity_ledger. If the curve is empty or flat we reconstruct a
    synthetic curve from closed-trade P&L so a thin paper history still penalizes
    losing streaks. No curve at all -> 0.0 drawdown (nothing to penalize yet).
    """
    try:
        curve = storage.equity_curve()
    except Exception:  # noqa: BLE001
        curve = []

    series: list[float] = []
    for row in curve:
        try:
            eq = row.get("equity")
            if eq is not None:
                series.append(float(eq))
        except (TypeError, ValueError):
            continue

    if len(series) < 2:
        # Reconstruct from closed trades if the ledger is thin.
        try:
            trades = storage.closed_trades()
        except Exception:  # noqa: BLE001
            trades = []
        running = 0.0
        synth = [0.0]
        for t in trades:
            try:
                pnl = t.get("pnl")
                if pnl is not None:
                    running += float(pnl)
                    synth.append(running)
            except (TypeError, ValueError):
                continue
        if len(synth) >= 2:
            # Offset by a base so the curve is positive (drawdown is relative to peak).
            base = abs(min(synth)) + 1.0
            series = [v + base for v in synth]

    if len(series) < 2:
        return 0.0

    peak = series[0]
    max_dd = 0.0
    for v in series:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
    return _clamp(max_dd, 0.0, 1.0)


def _safe_exp(x: float) -> float:
    import math
    try:
        # Clamp to avoid overflow on extreme inputs.
        return math.exp(max(-60.0, min(60.0, x)))
    except Exception:  # noqa: BLE001
        return 1.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
