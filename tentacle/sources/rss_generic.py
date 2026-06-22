"""Generic RSS tentacle (Tier 3, family=social, FREE).

Reads a comma-separated list of feed URLs from TENTACLE_RSS_FEEDS. This is the
hook for self-hosted RSSHub / Nitter / Telegram mirrors of fast-moving X accounts
(for example @DeItaone and similar breaking-news aggregators). If the env var is
unset, the source yields nothing and stays dormant.

Tickers are pulled aggressively from each entry's title + summary. These feeds
are unvetted social mirrors, so this is CONFIRMATION ONLY (pump risk):
catalyst_hint stays None and strength stays low.
"""
from __future__ import annotations

import os
from typing import Iterable

from ..models import RawSignal, SourceFamily, Tier
from ..util import extract_tickers, parse_rss
from .base import Source


class GenericRSS(Source):
    id = "rss_generic"
    family = SourceFamily.SOCIAL.value
    tier = Tier.SOCIAL.value
    requires_keys = ()  # free, but inert unless TENTACLE_RSS_FEEDS is set
    min_interval_seconds = 300

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(os.environ.get("TENTACLE_RSS_FEEDS", "").strip())

    def _feeds(self) -> list[str]:
        raw = os.environ.get("TENTACLE_RSS_FEEDS", "").strip()
        if not raw:
            return []
        return [u.strip() for u in raw.split(",") if u.strip()]

    def fetch(self) -> Iterable[RawSignal]:
        feeds = self._feeds()
        if not feeds:
            return
        for url in feeds:
            try:
                yield from self._scan(url)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("rss feed %s failed: %s", url, exc)
                continue

    def _scan(self, url: str) -> Iterable[RawSignal]:
        for e in parse_rss(url):
            title = e.get("title", "")
            summary = e.get("summary", "")
            text = f"{title} {summary}"
            tickers = extract_tickers(text, aggressive=True)
            if not tickers:
                continue  # only trade names we can map
            for sym in tickers:
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=title or f"RSS mention of {sym}",
                    url=e.get("link", ""),
                    summary=summary,
                    tickers=[sym],
                    event_time=e.get("published") or None,
                    signal_strength=0.25,
                    catalyst_hint=None,
                    raw={"feed": url},
                )
