"""Company newswire firehose tentacles (Tier 1, primary trust).

These poll the big press-release wires that companies use to distribute material
news (acquisitions, contract awards, FDA actions, earnings). Companies almost
always include their ticker in the boilerplate, e.g. "Acme Corp (NASDAQ: ACME)",
so extract_tickers can pin most releases to a US-listed name. Items that cannot
be mapped to a ticker are skipped (we only trade mapped names).

Coverage is best-effort: any single feed that 404s, rate-limits, or changes
format is skipped silently so the rest keep flowing. The wires below are the
public firehose endpoints as of writing; if a publisher changes their RSS path
the feed simply yields nothing.
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..models import Catalyst, RawSignal, SourceFamily, Tier
from ..util import extract_tickers, parse_rss
from .base import Source


def _hint_from_text(text: str) -> Optional[str]:
    """Map common press-release keywords to a catalyst category."""
    t = (text or "").lower()
    if any(k in t for k in ("to acquire", "acquisition of", "merger", "to be acquired",
                            "definitive agreement", "tender offer", "to merge")):
        return Catalyst.MNA_TARGET.value
    if any(k in t for k in ("awarded", "contract", "purchase order", "task order",
                            "wins ", "selected for")):
        return Catalyst.GOV_CONTRACT.value
    if any(k in t for k in ("fda", "approval", "approved", "phase 3", "phase iii",
                            "clearance", "510(k)")):
        return Catalyst.FDA_APPROVAL.value
    if any(k in t for k in ("beats estimates", "tops estimates", "beats expectations",
                            "exceeds estimates", "record revenue", "raises guidance")):
        return Catalyst.EARNINGS_BEAT.value
    return None


class _NewswireFeed(Source):
    """Base for a single company-newswire RSS firehose."""

    family = SourceFamily.NEWSWIRE.value
    tier = Tier.PRIMARY.value
    feed_url: str = ""
    min_interval_seconds = 120

    def fetch(self) -> Iterable[RawSignal]:
        if not self.feed_url:
            return
        for e in parse_rss(self.feed_url):
            title = e["title"]
            summary = e["summary"]
            text = f"{title} {summary}"
            tickers = extract_tickers(text)
            if not tickers:
                continue  # only trade names we can map to a US-listed ticker
            yield RawSignal(
                source_id=self.id,
                family=self.family,
                tier=self.tier,
                title=title,
                url=e["link"],
                summary=summary,
                tickers=tickers,
                event_time=e["published"],
                signal_strength=0.6,
                catalyst_hint=_hint_from_text(text),
                raw={"wire": self.id, "feed": self.feed_url},
            )


class GlobeNewswire(_NewswireFeed):
    id = "newswire_globenewswire"
    feed_url = (
        "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire"
    )


class PRNewswire(_NewswireFeed):
    id = "newswire_prnewswire"
    feed_url = "https://www.prnewswire.com/rss/news-releases-list.rss"


class Accesswire(_NewswireFeed):
    id = "newswire_accesswire"
    feed_url = "https://www.accesswire.com/users/rss.aspx"
