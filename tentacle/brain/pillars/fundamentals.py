"""Fundamentals pillar (best-effort, via yfinance Ticker.info).

Returns {"score": -1..1, "squeeze_boost": 0..0.15, "note": str}.

  score         : small fundamental sanity adjustment. Negative when the company
                  is burning cash with negative free cash flow (trust the catalyst
                  a little less). Mildly positive for healthy/profitable names.
  squeeze_boost : 0..0.15 extra conviction for a small float + high short interest
                  setup (a real catalyst into a crowded short can run further).
  note          : human-readable explanation.

Neutral ({"score":0, "squeeze_boost":0}) whenever data is unavailable. yfinance is
imported inside the function and guarded, so this module imports without it. Honest
note: Ticker(...).info is slow and sometimes incomplete; every field access is
defensive and any miss degrades to neutral rather than guessing.
"""
from __future__ import annotations

from typing import Any, Optional

from ...config import get_config


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _neutral(note: str) -> dict:
    return {"score": 0.0, "squeeze_boost": 0.0, "note": note}


def fundamentals_read(ticker: str, cfg=None, md=None) -> dict:
    """Best-effort fundamentals read. Never raises; neutral on any problem."""
    if cfg is None:
        cfg = get_config()
    if not ticker:
        return _neutral("no ticker")

    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return _neutral("yfinance unavailable")

    info = _safe_info(yf, ticker)
    if not info:
        return _neutral("no fundamentals info")

    try:
        score = 0.0
        notes: list[str] = []

        # --- Cash burn / negative free cash flow -> trust the catalyst less ---
        fcf = _num(info.get("freeCashflow"))
        total_cash = _num(info.get("totalCash"))
        if fcf is not None and fcf < 0:
            score -= 0.3
            notes.append("negative FCF")
            # Severe burn relative to cash on hand: a runway risk.
            if total_cash is not None and total_cash > 0:
                # annual burn vs cash; <1yr runway is a deeper negative.
                runway_years = total_cash / abs(fcf)
                if runway_years < 1.0:
                    score -= 0.2
                    notes.append(f"short runway ~{runway_years:.1f}y")
        elif fcf is not None and fcf > 0:
            score += 0.15
            notes.append("positive FCF")

        # Profitability sanity.
        margins = _num(info.get("profitMargins"))
        if margins is not None:
            if margins > 0.10:
                score += 0.1
                notes.append("healthy margins")
            elif margins < 0:
                score -= 0.1
                notes.append("unprofitable")

        # --- Squeeze setup: small float + high short interest ---
        squeeze_boost = 0.0
        float_shares = _num(info.get("floatShares"))
        short_pct_float = _num(info.get("shortPercentOfFloat"))
        short_ratio = _num(info.get("shortRatio"))  # days to cover

        small_float = float_shares is not None and float_shares < 50_000_000
        high_short = (
            (short_pct_float is not None and short_pct_float >= 0.15) or
            (short_ratio is not None and short_ratio >= 5.0)
        )
        if small_float and high_short:
            # Scale the boost by how extreme the short interest is, capped at 0.15.
            extra = 0.0
            if short_pct_float is not None:
                extra = _clamp((short_pct_float - 0.15) * 0.5, 0.0, 0.15)
            squeeze_boost = _clamp(max(0.10, extra), 0.0, 0.15)
            notes.append(
                f"squeeze setup (float<50M, "
                f"shortFloat={short_pct_float if short_pct_float is not None else 'na'})"
            )

        score = _clamp(score, -1.0, 1.0)
        squeeze_boost = _clamp(squeeze_boost, 0.0, 0.15)
        note = "; ".join(notes) if notes else "fundamentals neutral"
        return {"score": float(score), "squeeze_boost": float(squeeze_boost), "note": note}
    except Exception:
        return _neutral("fundamentals computation error")


def _safe_info(yf, ticker: str) -> Optional[dict]:
    """Fetch Ticker(...).info defensively. Returns a dict or None."""
    try:
        t = yf.Ticker(ticker)
        info = getattr(t, "info", None)
        if isinstance(info, dict) and info:
            return info
        # Some yfinance versions expose get_info().
        getter = getattr(t, "get_info", None)
        if callable(getter):
            info = getter()
            if isinstance(info, dict) and info:
                return info
    except Exception:
        return None
    return None


def _num(v: Any) -> Optional[float]:
    """Coerce to float, or None for missing/None/non-numeric values."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # yfinance occasionally returns NaN; reject it.
    if f != f:  # NaN check without importing math
        return None
    return f
