"""Market regime filter: only trade with the tape, never against it.

The regime is "up" when the benchmark's fast moving average is at or above its slow
moving average on daily closes. This is a coarse trend filter (config defaults: SPY,
20-day vs 50-day). If the data is unavailable for any reason we return
(False, "regime unknown") so an unknown tape BLOCKS trading. FAIL SAFE: we would
rather sit out than fire blind.
"""
from __future__ import annotations

from typing import Optional


def regime_is_up(cfg=None, md=None) -> tuple[bool, str]:
    """Return (ok, note). ok is True only when fast MA >= slow MA on daily closes.

    Data unavailable -> (False, "regime unknown"). Never raises.
    """
    try:
        if cfg is None:
            from ..config import get_config
            cfg = get_config()
        if md is None:
            from ..marketdata import get_marketdata
            md = get_marketdata()

        reg = cfg.regime
        benchmark = reg.benchmark
        fast = int(reg.fast_ma)
        slow = int(reg.slow_ma)
        need = max(fast, slow)

        # Pull enough history to cover the slow MA with a comfortable margin.
        df = md.daily_bars(benchmark, max(need + 10, 70))
        if df is None or len(df) < need:
            return (False, "regime unknown")

        try:
            closes = df["close"]
        except Exception:
            return (False, "regime unknown")

        try:
            fast_ma = float(closes.tail(fast).mean())
            slow_ma = float(closes.tail(slow).mean())
        except Exception:
            return (False, "regime unknown")

        # NaN guard: an unknown average must fail safe.
        if fast_ma != fast_ma or slow_ma != slow_ma:
            return (False, "regime unknown")

        if fast_ma >= slow_ma:
            return (True, f"{benchmark} uptrend fast({fast})={fast_ma:.2f} >= slow({slow})={slow_ma:.2f}")
        return (False, f"{benchmark} downtrend fast({fast})={fast_ma:.2f} < slow({slow})={slow_ma:.2f}")
    except Exception:
        # Any unexpected failure: unknown tape, block trading.
        return (False, "regime unknown")
