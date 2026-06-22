"""Kalshi prediction-market tentacle (family=crowd_forecast, Tier 5, FREE).

Kalshi exposes public markets without auth on its elections trade-api host. Like
Polymarket, a market that clearly maps to a public company is a soft LEAD that must
be confirmed by a real trigger family later. Confirm-only, low strength. Markets we
cannot map to a US ticker are skipped.
"""
from __future__ import annotations

from typing import Iterable

from ..models import RawSignal, SourceFamily, Tier
from ..util import extract_tickers, http_json, now_iso
from .base import Source
from ._watchlist import get_watchlist

_MARKETS_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"

_NAME_MAP = {
    "nvidia": "NVDA",
    "tesla": "TSLA",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "meta": "META",
    "palantir": "PLTR",
    "coinbase": "COIN",
    "microstrategy": "MSTR",
    "gamestop": "GME",
}


def _map_tickers(text: str, watch: list[str]) -> list[str]:
    found: list[str] = []
    for t in extract_tickers(text):
        if t in watch:
            found.append(t)
    low = (text or "").lower()
    for name, tk in _NAME_MAP.items():
        if name in low and tk not in found:
            found.append(tk)
    return found


class KalshiMarkets(Source):
    id = "kalshi"
    family = SourceFamily.CROWD_FORECAST.value
    tier = Tier.EXPANDED.value
    requires_keys: tuple[str, ...] = ()
    min_interval_seconds = 600

    def fetch(self) -> Iterable[RawSignal]:
        watch = list(get_watchlist(self.storage))
        try:
            data = http_json(_MARKETS_URL, params={"status": "open", "limit": 200})
        except Exception:
            return
        if not isinstance(data, dict):
            return
        markets = data.get("markets") or []
        if not isinstance(markets, list):
            return
        for m in markets:
            try:
                if not isinstance(m, dict):
                    continue
                title = m.get("title") or m.get("subtitle") or ""
                yes_sub = m.get("yes_sub_title") or ""
                tickers = _map_tickers(f"{title} {yes_sub}", watch)
                if not tickers:
                    continue
                # last_price is in cents (0..100), use as implied probability proxy
                last = m.get("last_price")
                implied = None
                try:
                    if last is not None:
                        implied = float(last) / 100.0
                except Exception:
                    implied = None
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"Kalshi: {title}"[:200],
                    url="https://kalshi.com/markets",
                    summary=yes_sub[:200],
                    tickers=tickers[:1],
                    event_time=m.get("open_time") or now_iso(),
                    signal_strength=0.25,
                    catalyst_hint=None,
                    raw={"implied_prob": implied, "ticker": m.get("ticker")},
                )
            except Exception:
                continue
