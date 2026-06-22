"""Technical pillar: read the price tape for one ticker.

Returns {"score": -1..1, "veto": bool, "note": str}.

  score  : a coarse trend/momentum read. Positive = constructive (uptrend, not
           overextended), negative = weak/extended.
  veto   : True when the price is extended at the highs into a bearish reversal
           (RSI(14) > 75 AND the last candle closes red below the prior close,
           near a 60-day high). A veto is a hard "do not chase into a blowoff top".
  note   : human-readable explanation.

Neutral (score 0, veto False) whenever data is unavailable: unknown data must not
manufacture a veto (a false block) nor inflate conviction (a false go). pandas is
imported inside the function and guarded, so this module imports even when pandas
is absent.
"""
from __future__ import annotations

from typing import Optional

from ...config import get_config


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _neutral(note: str) -> dict:
    return {"score": 0.0, "veto": False, "note": note}


def technical_read(ticker: str, cfg=None, md=None) -> dict:
    """Coarse technical read for `ticker`. Never raises; neutral on any problem."""
    if cfg is None:
        cfg = get_config()
    if md is None:
        try:
            from ...marketdata import get_marketdata
            md = get_marketdata()
        except Exception:
            return _neutral("no marketdata available")
    if not ticker:
        return _neutral("no ticker")

    try:
        import pandas as pd  # noqa: F401  (presence check; used implicitly via df ops)
    except Exception:
        return _neutral("pandas unavailable")

    try:
        df = md.daily_bars(ticker, 60)
    except Exception:
        return _neutral("daily_bars failed")
    if df is None or len(df) < 20:
        return _neutral("insufficient price history")

    try:
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2])

        # --- Trend: 20-day vs 50-ish MA (use what we have within 60 bars) ---
        n = len(close)
        fast_w = min(20, n)
        slow_w = min(50, n)
        ma_fast = float(close.tail(fast_w).mean())
        ma_slow = float(close.tail(slow_w).mean())
        trend_up = ma_fast >= ma_slow

        # --- RSI(14) (Wilder-style smoothing) ---
        rsi = _rsi(close, 14)

        # --- Extension vs the 60-day high ---
        hi_60 = float(high.max())
        near_high = hi_60 > 0 and last >= 0.97 * hi_60  # within 3% of the high

        last_red = last < prev  # closed below the prior close

        # --- Veto: blowoff-top reversal ---
        veto = bool(rsi is not None and rsi > 75 and last_red and near_high)

        # --- Score: blend trend + (de-)momentum, bounded to [-1, 1] ---
        score = 0.0
        score += 0.5 if trend_up else -0.5
        if rsi is not None:
            if rsi >= 70:
                score -= 0.3        # overbought, fade conviction
            elif rsi <= 30:
                score -= 0.2        # oversold weakness (we are long-only; not a buy edge)
            elif 45 <= rsi <= 65:
                score += 0.2        # healthy momentum band
        if last_red and near_high:
            score -= 0.2            # reversal candle at the highs
        score = _clamp(score, -1.0, 1.0)

        note = (
            f"trend={'up' if trend_up else 'down'} "
            f"rsi={rsi:.0f} " if rsi is not None else
            f"trend={'up' if trend_up else 'down'} rsi=na "
        )
        note += f"near_high={near_high} lastRed={last_red}"
        if veto:
            note = "VETO blowoff reversal at highs: " + note
        return {"score": float(score), "veto": veto, "note": note}
    except Exception:
        return _neutral("technical computation error")


def _rsi(close, period: int = 14) -> Optional[float]:
    """Wilder RSI of the last bar, or None if not computable."""
    try:
        if len(close) <= period:
            return None
        delta = close.diff().dropna()
        gains = delta.clip(lower=0.0)
        losses = (-delta).clip(lower=0.0)
        # Wilder smoothing via exponential weighting with alpha = 1/period.
        avg_gain = gains.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
        avg_loss = losses.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
        avg_gain = float(avg_gain)
        avg_loss = float(avg_loss)
        if avg_loss == 0.0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
    except Exception:
        return None
