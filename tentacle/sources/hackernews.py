"""Hacker News front-page tentacle (family=crowd_forecast, Tier 5, FREE).

Uses the free Algolia HN search API. We read front-page stories, run the title
through the shared theme detector, and map any detected theme to its proxy ticker
basket. This is confirm-only thematic (a hot AI/quantum/semis story nudges the whole
basket), very low strength. Stories with no detectable theme are skipped.
"""
from __future__ import annotations

from typing import Iterable

from ..models import RawSignal, SourceFamily, Tier
from ..util import http_json, now_iso, parse_dt
from .base import Source
from ._watchlist import detect_themes, tickers_for_theme

_SEARCH_URL = "https://hn.algolia.com/api/v1/search"

# A front-page story needs real traction to count as a crowd signal.
_MIN_POINTS = 100


class HackerNewsFrontPage(Source):
    id = "hackernews"
    family = SourceFamily.CROWD_FORECAST.value
    tier = Tier.EXPANDED.value
    requires_keys: tuple[str, ...] = ()
    min_interval_seconds = 1800

    def fetch(self) -> Iterable[RawSignal]:
        try:
            data = http_json(
                _SEARCH_URL,
                params={"tags": "front_page", "hitsPerPage": 50},
            )
        except Exception:
            return
        if not isinstance(data, dict):
            return
        hits = data.get("hits") or []
        if not isinstance(hits, list):
            return
        for hit in hits:
            try:
                if not isinstance(hit, dict):
                    continue
                title = hit.get("title") or hit.get("story_title") or ""
                points = int(hit.get("points") or 0)
                if points < _MIN_POINTS:
                    continue
                themes = detect_themes(title)
                if not themes:
                    continue
                tickers: list[str] = []
                for th in themes:
                    for tk in tickers_for_theme(th):
                        if tk not in tickers:
                            tickers.append(tk)
                if not tickers:
                    continue
                created = hit.get("created_at")
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"HN front page: {title}"[:200],
                    url=hit.get("url") or (
                        f"https://news.ycombinator.com/item?id={hit.get('objectID')}"),
                    summary=f"{points} points; themes: {', '.join(themes)}.",
                    tickers=tickers,
                    event_time=(parse_dt(created).isoformat() if parse_dt(created) else now_iso()),
                    signal_strength=0.18,
                    catalyst_hint=None,
                    raw={"points": points, "themes": themes},
                )
            except Exception:
                continue
