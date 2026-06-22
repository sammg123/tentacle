"""Self-tuning: nudge per-source reliability and per-catalyst priors from outcomes.

This is the slow, conservative learning step. From the resolved-outcome record:

  * Per source_id: compute the win rate of every outcome that source contributed to,
    then EMA the source's tuned_weight toward (0.5 + winrate), bounded to [0.3, 1.5].
    A source whose signals tend to precede winners gets trusted more, a noisy one less.
  * Per catalyst: update the drift_prior toward a value derived from the average
    realized drift, bounded to [0.7, 1.3].

We require a minimum sample (>= 8) before moving anything, and we move slowly (EMA),
so a handful of lucky or unlucky outcomes cannot whipsaw the weights. Everything is
guarded: a tuning error never escapes into the engine loop.
"""
from __future__ import annotations

from typing import Optional

from ..config import Config, get_config
from ..models import OutcomeLabel
from ..util import get_logger

log = get_logger("tentacle.learning.tuner")

# Minimum resolved outcomes touching a thing before we are willing to move it.
_MIN_SAMPLE = 8
# EMA blend: new_value = (1-alpha)*old + alpha*target. Small alpha == slow learning.
_EMA_ALPHA = 0.20
# Hard bounds the learning loop may never cross (the config defaults are the anchor).
_WEIGHT_LO, _WEIGHT_HI = 0.3, 1.5
_PRIOR_LO, _PRIOR_HI = 0.7, 1.3


def retune(storage, cfg: Optional[Config] = None) -> dict:
    """Nudge source weights and catalyst priors from resolved outcomes.

    Returns a summary dict: {"sources": {id: {...}}, "catalysts": {cat: {...}},
    "n_outcomes": int, "note": str}. On any failure returns a safe empty summary so
    the caller can proceed; tuning is best-effort and never required for safety.
    """
    cfg = cfg or get_config()
    summary: dict = {"sources": {}, "catalysts": {}, "n_outcomes": 0, "note": ""}

    try:
        rows = storage.resolved_outcomes()
    except Exception as exc:  # noqa: BLE001
        log.debug("resolved_outcomes failed: %s", exc)
        summary["note"] = "no resolved outcomes available"
        return summary

    rows = [r for r in rows if r.get("label") != OutcomeLabel.PENDING.value]
    summary["n_outcomes"] = len(rows)
    if not rows:
        summary["note"] = "no resolved outcomes"
        return summary

    # ----------------------------------------------------------------- sources
    # Aggregate per source_id: how many outcomes it touched and how many were wins.
    src_total: dict[str, int] = {}
    src_wins: dict[str, int] = {}
    for r in rows:
        is_win = r.get("label") == OutcomeLabel.WIN.value
        for sid in _loads_list(r.get("source_ids")):
            src_total[sid] = src_total.get(sid, 0) + 1
            if is_win:
                src_wins[sid] = src_wins.get(sid, 0) + 1

    for sid, total in src_total.items():
        if total < _MIN_SAMPLE:
            continue
        try:
            winrate = src_wins.get(sid, 0) / total
            target = _clamp(0.5 + winrate, _WEIGHT_LO, _WEIGHT_HI)
            old = storage.get_source_weight(sid)
            try:
                old = float(old)
            except (TypeError, ValueError):
                old = 1.0
            new = _clamp((1 - _EMA_ALPHA) * old + _EMA_ALPHA * target, _WEIGHT_LO, _WEIGHT_HI)
            storage.set_source_weight(sid, new)
            summary["sources"][sid] = {
                "old": round(old, 4),
                "new": round(new, 4),
                "winrate": round(winrate, 4),
                "samples": total,
            }
        except Exception as exc:  # noqa: BLE001
            log.debug("source tune %s failed: %s", sid, exc)
            continue

    # --------------------------------------------------------------- catalysts
    cat_total: dict[str, int] = {}
    cat_wins: dict[str, int] = {}
    cat_drift_sum: dict[str, float] = {}
    for r in rows:
        cat = r.get("catalyst") or ""
        if not cat:
            continue
        cat_total[cat] = cat_total.get(cat, 0) + 1
        if r.get("label") == OutcomeLabel.WIN.value:
            cat_wins[cat] = cat_wins.get(cat, 0) + 1
        try:
            d = r.get("drift_pct")
            if d is not None:
                cat_drift_sum[cat] = cat_drift_sum.get(cat, 0.0) + float(d)
        except (TypeError, ValueError):
            pass

    for cat, total in cat_total.items():
        if total < _MIN_SAMPLE:
            continue
        try:
            avg_drift = cat_drift_sum.get(cat, 0.0) / total
            # Map average realized drift into a multiplier centered on 1.0. A +5%
            # average drift over the horizon nudges the prior up by ~0.5*5% scaled.
            # We scale modestly: a strong +10% average maps near the +1.3 ceiling.
            target = _clamp(1.0 + avg_drift * 3.0, _PRIOR_LO, _PRIOR_HI)
            old = storage.get_catalyst_prior(cat)
            if old is None:
                from ..config import catalyst_spec
                old = catalyst_spec(cat).drift_prior
            try:
                old = float(old)
            except (TypeError, ValueError):
                old = 1.0
            new = _clamp((1 - _EMA_ALPHA) * old + _EMA_ALPHA * target, _PRIOR_LO, _PRIOR_HI)
            wins = cat_wins.get(cat, 0)
            # Persist the new prior. samples/wins/sum_drift accumulate in storage;
            # we pass deltas of 0 for those running tallies to avoid double counting
            # (this function recomputes from the full resolved set each pass).
            storage.upsert_catalyst_prior(cat, new, samples=0, wins=0, sum_drift=0.0)
            summary["catalysts"][cat] = {
                "old": round(old, 4),
                "new": round(new, 4),
                "avg_drift": round(avg_drift, 4),
                "winrate": round(wins / total, 4),
                "samples": total,
            }
        except Exception as exc:  # noqa: BLE001
            log.debug("catalyst tune %s failed: %s", cat, exc)
            continue

    summary["note"] = (
        f"tuned {len(summary['sources'])} sources, "
        f"{len(summary['catalysts'])} catalysts from {len(rows)} outcomes"
    )
    return summary


def _loads_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        import json
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
    except Exception:  # noqa: BLE001
        pass
    return []


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
