"""GDELT news tentacle (Tier 2, news family).

GDELT Doc 2.0 is a free, keyless global news index. We query a handful of broad
market terms and pull recent article titles, then extract tickers from the
titles via cashtags / exchange parens. GDELT is breadth-not-depth: titles are
short and rarely carry a ticker, so this is modest by design (strength ~0.45).
Articles with no detectable ticker are skipped. Everything is wrapped so a
flaky response never raises.
"""
from __future__ import annotations

from typing import Iterable

from ..models import RawSignal, SourceFamily, Tier
from ..util import extract_tickers, http_json
from .base import Source

_QUERIES = [
    "stock surge",
    "acquisition deal",
    "FDA approval",
    "earnings beat",
]


class Gdelt(Source):
    id = "gdelt_news"
    family = SourceFamily.NEWS.value
    tier = Tier.NEWS_API.value
    min_interval_seconds = 300

    def fetch(self) -> Iterable[RawSignal]:
        base = "https://api.gdeltproject.org/api/v2/doc/doc"
        for q in _QUERIES:
            params = {
                "query": q,
                "mode": "ArtList",
                "format": "json",
                "maxrecords": 50,
                "sort": "datedesc",
            }
            data = http_json(base, params=params)
            if not isinstance(data, dict):
                continue
            for art in data.get("articles", []) or []:
                try:
                    title = art.get("title", "") or ""
                    tickers = extract_tickers(title)
                    if not tickers:
                        continue
                    yield RawSignal(
                        source_id=self.id,
                        family=self.family,
                        tier=self.tier,
                        title=title,
                        url=art.get("url", "") or "",
                        summary=art.get("domain", "") or "",
                        tickers=tickers,
                        event_time=art.get("seendate"),
                        signal_strength=0.45,
                        catalyst_hint=None,
                        raw={"query": q, "domain": art.get("domain")},
                    )
                except Exception:
                    continue
