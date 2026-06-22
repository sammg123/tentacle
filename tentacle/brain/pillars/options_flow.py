"""Options-flow pillar.

Returns {"score": -1..1, "note": str}.

Honest limitation: there is no clean, free, realtime options feed we can lean on,
so this pillar defaults to NEUTRAL (score 0). It exists as a stable hook so that
when a flow source (e.g. unusual-whales-style) tags a ticker via storage, this
pillar can reflect that corroboration. Per the weighting law, the FLOW family is
confirm-only: it can nudge conviction but never trigger a trade by itself, so this
pillar only ever contributes a bounded, modest score.

The optional `storage` lookup below is best-effort: if a future flow source writes
a per-ticker flow score into the kv store under "flow:<TICKER>", we read it; on any
problem we stay neutral. We never raise.
"""
from __future__ import annotations

from typing import Optional

from ...config import get_config


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def options_flow_read(ticker: str, cfg=None) -> dict:
    """Neutral by default; reflect a flow tag if one is present in storage."""
    if cfg is None:
        cfg = get_config()
    if not ticker:
        return {"score": 0.0, "note": "no ticker"}

    score = _flow_tag_score(ticker)
    if score is None:
        return {
            "score": 0.0,
            "note": ("neutral: no free realtime options feed; flow is "
                     "confirm-only and currently untagged"),
        }
    score = _clamp(float(score), -1.0, 1.0)
    return {"score": score, "note": f"flow tag for {ticker}: {score:+.2f}"}


def _flow_tag_score(ticker: str) -> Optional[float]:
    """Best-effort read of a per-ticker flow score from the kv store.

    A future flow tentacle may write kv "flow:<TICKER>" as a number in [-1, 1]
    (bullish call sweeps positive, bearish put flow negative). Returns None when
    nothing is tagged or anything goes wrong (stay neutral).
    """
    try:
        from ...storage import get_storage
        storage = get_storage()
        val = storage.kv_get(f"flow:{ticker.upper().strip()}")
        if val is None:
            return None
        return float(val)
    except Exception:
        return None
