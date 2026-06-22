"""Polymarket prediction-market tentacle (family=crowd_forecast, Tier 5, FREE).

Polymarket's Gamma API exposes open markets for free. A shift in the implied odds
of a market that clearly maps to a public company (e.g. an earnings/M&A/approval
market that names a cashtag or a known company name) is a soft LEAD. It must be
confirmed by a Tier 1-2 trigger family later, so this stays confirm-only at low
strength. Markets that cannot be mapped to a US ticker are skipped.
"""
from __future__ import annotations

from typing import Iterable

from ..models import RawSignal, SourceFamily, Tier
from ..util import extract_tickers, http_json, now_iso
from .base import Source
from ._watchlist import get_watchlist

_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

# A few well-known names whose plain English mention is unambiguous enough to map.
# Kept tiny and conservative to avoid phantom signals.
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
    # explicit cashtags / exchange tags are reliable
    for t in extract_tickers(text):
        if t in watch:
            found.append(t)
    low = (text or "").lower()
    for name, tk in _NAME_MAP.items():
        if name in low and tk not in found:
            found.append(tk)
    return found


class PolymarketMarkets(Source):
    id = "polymarket"
    family = SourceFamily.CROWD_FORECAST.value
    tier = Tier.EXPANDED.value
    requires_keys: tuple[str, ...] = ()
    min_interval_seconds = 600

    def fetch(self) -> Iterable[RawSignal]:
        watch = set(get_watchlist(self.storage))
        try:
            data = http_json(_MARKETS_URL, params={"closed": "false", "limit": 100})
        except Exception:
            return
        if not isinstance(data, list):
            return
        for m in data:
            try:
                if not isinstance(m, dict):
                    continue
                question = m.get("question") or m.get("title") or ""
                desc = m.get("description") or ""
                tickers = _map_tickers(f"{question} {desc}", list(watch))
                if not tickers:
                    continue
                # use the best (yes-token) price as the implied probability if present
                price = None
                try:
                    prices = m.get("outcomePrices") or m.get("outcome_prices")
                    if isinstance(prices, list) and prices:
                        price = float(prices[0])
                    elif isinstance(prices, str):
                        import json as _json
                        pl = _json.loads(prices)
                        if pl:
                            price = float(pl[0])
                except Exception:
                    price = None
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"Polymarket: {question}"[:200],
                    url=("https://polymarket.com/event/" + str(m.get("slug")))
                    if m.get("slug") else "",
                    summary=desc[:200],
                    tickers=tickers[:1],
                    event_time=m.get("startDate") or now_iso(),
                    signal_strength=0.25,
                    catalyst_hint=None,
                    raw={"implied_prob": price, "id": m.get("id")},
                )
            except Exception:
                continue
