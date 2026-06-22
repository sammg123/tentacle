"""Google Trends tentacle (family=crowd_forecast, Tier 5, confirm-only).

Rising search interest for a watchlist company name is a soft attention confirm.
This uses pytrends, an UNOFFICIAL Google Trends client, imported lazily and guarded:
if it is not installed (or Google rate-limits us) the tentacle yields nothing rather
than crash. Confirm-only, low strength. We only emit when the most recent interest
clearly exceeds the trailing mean for that term.
"""
from __future__ import annotations

from typing import Iterable

from ..models import RawSignal, SourceFamily, Tier
from ..util import now_iso
from .base import Source
from ._watchlist import get_watchlist

# Map a small set of watchlist tickers to their search term. Only names where the
# company name is a clean, unambiguous query are worth polling.
_TERMS: dict[str, str] = {
    "NVDA": "Nvidia",
    "TSLA": "Tesla stock",
    "PLTR": "Palantir",
    "COIN": "Coinbase",
    "MSTR": "MicroStrategy",
    "GME": "GameStop",
    "AMC": "AMC stock",
    "SMCI": "Super Micro",
    "IONQ": "IonQ",
    "RKLB": "Rocket Lab",
}


class GoogleTrends(Source):
    id = "google_trends"
    family = SourceFamily.CROWD_FORECAST.value
    tier = Tier.EXPANDED.value
    requires_keys: tuple[str, ...] = ()
    min_interval_seconds = 3600

    def fetch(self) -> Iterable[RawSignal]:
        try:
            from pytrends.request import TrendReq  # type: ignore
        except Exception:
            # pytrends not installed: tentacle stays quiet.
            return
        watch = set(get_watchlist(self.storage))
        terms = {tk: q for tk, q in _TERMS.items() if tk in watch}
        if not terms:
            return
        try:
            pytrends = TrendReq(hl="en-US", tz=0)
        except Exception:
            return
        for ticker, term in terms.items():
            try:
                pytrends.build_payload([term], timeframe="now 7-d")
                df = pytrends.interest_over_time()
                if df is None or df.empty or term not in df.columns:
                    continue
                series = df[term].tolist()
                if len(series) < 8:
                    continue
                recent = series[-1]
                trailing = series[:-1]
                mean = sum(trailing) / len(trailing) if trailing else 0.0
                if mean <= 0 or recent <= mean * 1.5:
                    continue
                ratio = recent / mean
                strength = max(0.15, min(0.4, 0.15 + (ratio - 1.5) * 0.1))
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"Search interest spike: {term}",
                    url="https://trends.google.com/trends/explore",
                    summary=f"'{term}' search interest {recent} vs trailing mean {mean:.0f}.",
                    tickers=[ticker],
                    event_time=now_iso(),
                    signal_strength=strength,
                    catalyst_hint=None,
                    raw={"recent": recent, "trailing_mean": mean, "term": term},
                )
            except Exception:
                continue
