"""Shared market-data access: prices, bars, VWAP, dollar-volume, and the clock.

Prefers the Alpaca data API (free IEX feed) when keys are present, falls back to
yfinance (no key) otherwise. Everything degrades gracefully to ``None`` so a data
hiccup never fires or blocks a trade by accident; callers treat ``None`` as
"unknown" and the risk gates fail safe.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from .config import Config, get_config
from .util import get_logger, now_utc

log = get_logger("tentacle.marketdata")

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone(timedelta(hours=-5))

try:
    import yfinance as yf  # type: ignore
except Exception:  # pragma: no cover
    yf = None  # type: ignore

# US market holidays (NYSE/Nasdaq). Used only as the fallback when the Alpaca
# clock is unavailable. Update yearly; the Alpaca clock is authoritative when keyed.
_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


class MarketData:
    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or get_config()
        i = self.cfg.integrations
        self._alpaca = bool(i.alpaca_key and i.alpaca_secret)
        self._headers = {
            "APCA-API-KEY-ID": i.alpaca_key,
            "APCA-API-SECRET-KEY": i.alpaca_secret,
        }

    # ------------------------------------------------------------------ clock
    def is_market_open(self) -> bool:
        """True only during regular US equity hours (never off-hours/weekend/holiday)."""
        if self._alpaca:
            from .util import http_json
            data = http_json(
                f"{self.cfg.integrations.alpaca_trade_base}/v2/clock",
                headers=self._headers,
            )
            if isinstance(data, dict) and "is_open" in data:
                return bool(data["is_open"])
        return self._computed_market_open()

    def _computed_market_open(self, when: Optional[datetime] = None) -> bool:
        now = (when or now_utc()).astimezone(_ET)
        if now.weekday() >= 5:                       # Sat/Sun
            return False
        if now.date().isoformat() in _HOLIDAYS:
            return False
        return time(9, 30) <= now.time() <= time(16, 0)

    # ------------------------------------------------------------------ last price
    def last_price(self, ticker: str) -> Optional[float]:
        if self._alpaca:
            from .util import http_json
            data = http_json(
                f"{self.cfg.integrations.alpaca_data_base}/v2/stocks/{ticker}/trades/latest",
                headers=self._headers, params={"feed": "iex"},
            )
            try:
                return float(data["trade"]["p"])
            except Exception:
                pass
        return self._yf_last(ticker)

    def _yf_last(self, ticker: str) -> Optional[float]:
        if yf is None:
            return None
        try:
            t = yf.Ticker(ticker)
            fi = getattr(t, "fast_info", None)
            if fi and fi.get("last_price"):
                return float(fi["last_price"])
            hist = t.history(period="1d")
            if len(hist):
                return float(hist["Close"].iloc[-1])
        except Exception:
            return None
        return None

    # ------------------------------------------------------------------ bars
    def daily_bars(self, ticker: str, days: int = 120):
        """Return a pandas DataFrame of daily OHLCV (lowercase cols) or None."""
        if self._alpaca:
            df = self._alpaca_daily(ticker, days)
            if df is not None and len(df):
                return df
        return self._yf_daily(ticker, days)

    def _alpaca_daily(self, ticker: str, days: int):
        from .util import http_json
        start = (now_utc() - timedelta(days=int(days * 1.6) + 5)).date().isoformat()
        data = http_json(
            f"{self.cfg.integrations.alpaca_data_base}/v2/stocks/{ticker}/bars",
            headers=self._headers,
            params={"timeframe": "1Day", "start": start, "limit": 10000, "feed": "iex"},
        )
        try:
            import pandas as pd
            bars = data.get("bars") or []
            if not bars:
                return None
            df = pd.DataFrame(bars)
            df = df.rename(columns={"o": "open", "h": "high", "l": "low",
                                    "c": "close", "v": "volume", "t": "ts"})
            df["ts"] = pd.to_datetime(df["ts"])
            df = df.set_index("ts")
            return df[["open", "high", "low", "close", "volume"]].tail(days)
        except Exception:
            return None

    def _yf_daily(self, ticker: str, days: int):
        if yf is None:
            return None
        try:
            import pandas as pd
            period = f"{max(days + 10, 30)}d"
            hist = yf.Ticker(ticker).history(period=period)
            if not len(hist):
                return None
            hist = hist.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                        "Close": "close", "Volume": "volume"})
            return hist[["open", "high", "low", "close", "volume"]].tail(days)
        except Exception:
            return None

    # ------------------------------------------------------------------ vwap / liquidity
    def snapshot(self, ticker: str) -> Optional[dict]:
        if not self._alpaca:
            return None
        from .util import http_json
        return http_json(
            f"{self.cfg.integrations.alpaca_data_base}/v2/stocks/{ticker}/snapshot",
            headers=self._headers, params={"feed": "iex"},
        )

    def intraday_vwap(self, ticker: str) -> Optional[float]:
        snap = self.snapshot(ticker)
        try:
            return float(snap["dailyBar"]["vw"])
        except Exception:
            pass
        # fallback: typical price of last daily bar
        df = self.daily_bars(ticker, 1)
        if df is not None and len(df):
            row = df.iloc[-1]
            return float((row["high"] + row["low"] + row["close"]) / 3)
        return None

    def avg_dollar_volume(self, ticker: str, days: int = 20) -> Optional[float]:
        df = self.daily_bars(ticker, days)
        if df is None or not len(df):
            return None
        try:
            tp = (df["high"] + df["low"] + df["close"]) / 3
            return float((tp * df["volume"]).tail(days).mean())
        except Exception:
            return None


_MD: Optional[MarketData] = None


def get_marketdata() -> MarketData:
    global _MD
    if _MD is None:
        _MD = MarketData()
    return _MD
