"""Wikipedia pageviews tentacle (family=crowd_forecast, Tier 5, FREE).

A spike in daily pageviews for a company's Wikipedia article vs its trailing mean is
a soft attention confirm. Uses the free Wikimedia pageviews REST API. Confirm-only,
low strength. Only watchlist names with a known, clean wiki page title are polled.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Iterable

from ..models import RawSignal, SourceFamily, Tier
from ..util import http_json, now_iso, now_utc
from .base import Source
from ._watchlist import get_watchlist

_BASE = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
    "en.wikipedia/all-access/user/{page}/daily/{start}/{end}"
)

# Watchlist ticker -> Wikipedia article title (underscores, as the API expects).
_PAGES: dict[str, str] = {
    "NVDA": "Nvidia",
    "TSLA": "Tesla,_Inc.",
    "AAPL": "Apple_Inc.",
    "MSFT": "Microsoft",
    "AMZN": "Amazon_(company)",
    "META": "Meta_Platforms",
    "GOOGL": "Google",
    "PLTR": "Palantir_Technologies",
    "COIN": "Coinbase",
    "MSTR": "MicroStrategy",
    "GME": "GameStop",
    "AMD": "Advanced_Micro_Devices",
    "INTC": "Intel",
    "SMCI": "Supermicro",
    "IONQ": "IonQ",
    "RKLB": "Rocket_Lab",
}


class WikipediaPageviews(Source):
    id = "wikipedia"
    family = SourceFamily.CROWD_FORECAST.value
    tier = Tier.EXPANDED.value
    requires_keys: tuple[str, ...] = ()
    min_interval_seconds = 7200

    def fetch(self) -> Iterable[RawSignal]:
        watch = set(get_watchlist(self.storage))
        pages = {tk: p for tk, p in _PAGES.items() if tk in watch}
        if not pages:
            return
        end = (now_utc() - timedelta(days=1)).strftime("%Y%m%d")
        start = (now_utc() - timedelta(days=21)).strftime("%Y%m%d")
        for ticker, page in pages.items():
            try:
                url = _BASE.format(page=page, start=start, end=end)
                data = http_json(url, headers={"Accept": "application/json"})
                if not isinstance(data, dict):
                    continue
                items = data.get("items") or []
                views = [int(it.get("views", 0)) for it in items if isinstance(it, dict)]
                if len(views) < 8:
                    continue
                recent = views[-1]
                trailing = views[:-1]
                mean = sum(trailing) / len(trailing) if trailing else 0.0
                if mean <= 0 or recent <= mean * 2.0:
                    continue
                ratio = recent / mean
                strength = max(0.15, min(0.4, 0.15 + (ratio - 2.0) * 0.05))
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"Wikipedia views spike: {page.replace('_', ' ')}",
                    url=f"https://en.wikipedia.org/wiki/{page}",
                    summary=f"{recent} views vs trailing mean {mean:.0f} ({ratio:.1f}x).",
                    tickers=[ticker],
                    event_time=now_iso(),
                    signal_strength=strength,
                    catalyst_hint=None,
                    raw={"recent": recent, "trailing_mean": mean},
                )
            except Exception:
                continue
