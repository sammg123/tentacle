"""Index add/delete tentacles (Tier 1, index family).

Index rebalances are the single most mechanical catalyst: when a name is ADDED
to a major index, passive funds are forced to buy it, producing reliable upward
drift into the effective date. We watch the official press channels:
  * S&P Dow Jones Indices announcements (RSS / news search).
  * Nasdaq index change press releases (RSS / news search).

We try to parse the ADDED company's ticker from the release text via the usual
cashtag / exchange-paren patterns. Releases that announce both adds and deletes
are common; we keep any detectable ticker and let the brain sort direction (we
are long-only, so a "deleted" name simply will not pass confirmation upward).
This feed is OFTEN SPARSE, which is expected: rebalances happen on a schedule.
signal_strength is high (~0.8) because the mechanism is well understood.
"""
from __future__ import annotations

from typing import Iterable

from ..models import Catalyst, RawSignal, SourceFamily, Tier
from ..util import extract_tickers, parse_rss
from .base import Source

# Only treat a release as an inclusion event if it actually talks about adds.
_ADD_TERMS = ("will be added", "set to join", "added to", "to join the",
              "joining the", "additions to", "will replace", "set to be added",
              "will join")


class _IndexFeed(Source):
    family = SourceFamily.INDEX.value
    tier = Tier.PRIMARY.value
    catalyst_hint = Catalyst.INDEX_INCLUSION.value
    min_interval_seconds = 600
    feed_url: str = ""

    def fetch(self) -> Iterable[RawSignal]:
        if not self.feed_url:
            return
        for e in parse_rss(self.feed_url):
            title = e["title"]
            summary = e["summary"]
            text = f"{title} {summary}"
            low = text.lower()
            if not any(term in low for term in _ADD_TERMS):
                continue  # not an inclusion event, skip (deletes do not help long-only)
            tickers = extract_tickers(text)
            if not tickers:
                continue
            yield RawSignal(
                source_id=self.id,
                family=self.family,
                tier=self.tier,
                title=title,
                url=e["link"],
                summary=summary,
                tickers=tickers,
                event_time=e["published"],
                signal_strength=0.8,
                catalyst_hint=self.catalyst_hint,
                raw={"feed": self.feed_url},
            )


class SPDJIndexChanges(_IndexFeed):
    id = "index_spdji"
    # S&P Dow Jones Indices announcements feed (Google News fallback search keeps
    # it resilient if the official RSS path moves).
    feed_url = (
        "https://news.google.com/rss/search?"
        "q=%22S%26P%20500%22%20(added%20OR%20%22will%20join%22)%20index&hl=en-US&gl=US&ceid=US:en"
    )


class NasdaqIndexChanges(_IndexFeed):
    id = "index_nasdaq"
    feed_url = (
        "https://news.google.com/rss/search?"
        "q=%22Nasdaq-100%22%20(added%20OR%20%22will%20join%22)&hl=en-US&gl=US&ceid=US:en"
    )
