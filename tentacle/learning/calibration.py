"""Calibration error: how honest is the bot's confidence?

A well-calibrated model that says "80% confident" should win about 80% of the time.
We bucket resolved outcomes by predicted confidence (deciles), compare the predicted
win probability (confidence / 100) to the realized win rate in that bucket, and report
the mean absolute gap. 0.0 == perfectly calibrated, 1.0 == maximally overconfident.

Mastery uses (1 - calibration_error) so overconfidence is punished directly. With no
resolved history we return 0.5 (unknown), which keeps mastery honest and low at start.
"""
from __future__ import annotations

from typing import Optional

from ..config import Config, get_config
from ..models import OutcomeLabel
from ..util import get_logger

log = get_logger("tentacle.learning.calibration")


def calibration_error(storage, cfg: Optional[Config] = None) -> float:
    """Mean absolute gap between predicted win-prob and realized win rate, 0..1.

    Empty / no resolved history -> 0.5 (unknown). Flat outcomes are not wins (a flat
    move captured none of the predicted edge), so they count against the realized
    win rate in their bucket. Errors are swallowed and reported as 0.5.
    """
    cfg = cfg or get_config()
    try:
        rows = storage.resolved_outcomes()
    except Exception as exc:  # noqa: BLE001
        log.debug("resolved_outcomes failed: %s", exc)
        return 0.5

    if not rows:
        return 0.5

    # Ten confidence deciles: [0,10), [10,20), ... [90,100].
    buckets: dict[int, list[tuple[float, bool]]] = {i: [] for i in range(10)}
    used = 0
    for r in rows:
        try:
            conf = float(r.get("confidence") or 0.0)
        except (TypeError, ValueError):
            continue
        conf = _clamp(conf, 0.0, 100.0)
        label = r.get("label")
        if label == OutcomeLabel.PENDING.value:
            continue
        is_win = label == OutcomeLabel.WIN.value
        idx = int(conf // 10)
        if idx > 9:
            idx = 9
        buckets[idx].append((conf / 100.0, is_win))
        used += 1

    if used == 0:
        return 0.5

    # Sample-weighted mean absolute gap across non-empty buckets.
    total_weight = 0
    weighted_gap = 0.0
    for items in buckets.values():
        n = len(items)
        if n == 0:
            continue
        predicted = sum(p for p, _ in items) / n
        realized = sum(1 for _, w in items if w) / n
        weighted_gap += abs(predicted - realized) * n
        total_weight += n

    if total_weight == 0:
        return 0.5
    return _clamp(weighted_gap / total_weight, 0.0, 1.0)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
