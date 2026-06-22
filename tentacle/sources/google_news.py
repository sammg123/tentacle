"""Per-ticker Google News RSS tentacle (Tier 2, news family).

Google News exposes a free RSS search. We iterate a bounded watchlist (see
sources/_watchlist.get_watchlist) and, for each ticker, fetch its recent news
headlines. Because we already know which ticker the query is about, we set
tickers=[that ticker] directly rather than relying on extraction. catalyst_hint
is inferred from headline keywords. Strength is modest (~0.4): general news is a
confirmer, not a primary trigger. The watchlist is capped to stay polite.
"""
from __future__ import annotations

import urllib.parse
from typing import Iterable, Optional

from ..models import Catalyst, RawSignal, SourceFamily, Tier
from ..util import parse_rss
from ._watchlist import get_watchlist
from .base import Source


def _hint_from_text(text: str) -> Optional[str]:
    t = (text or "").lower()
    if any(k in t for k in ("to acquire", "acquisition", "merger", "to be acquired",
                            "takeover", "buyout")):
        return Catalyst.MNA_TARGET.value
    if any(k in t for k in ("fda", "approval", "approved", "phase 3", "phase iii")):
        return Catalyst.FDA_APPROVAL.value
    if any(k in t for k in ("beats estimates", "tops estimates", "beats expectations",
                            "earnings beat", "raises guidance")):
        return Catalyst.EARNINGS_BEAT.value
    if any(k in t for k in ("added to", "joins the s&p", "index inclusion",
                            "to join the")):
        return Catalyst.INDEX_INCLUSION.value
    if any(k in t for k in ("awarded", "contract", "task order")):
        return Catalyst.GOV_CONTRACT.value
    return None


class GoogleNews(Source):
    id = "google_news"
    family = SourceFamily.NEWS.value
    tier = Tier.NEWS_API.value
    min_interval_seconds = 600
    # cap how many headlines per ticker we keep, to keep volume sane
    max_items_per_ticker = 5

    def fetch(self) -> Iterable[RawSignal]:
        watchlist = get_watchlist(self.storage)
        for ticker in watchlist:
            q = urllib.parse.quote(f'"{ticker}" stock')
            url = (
                f"https://news.google.com/rss/search?q={q}"
                "&hl=en-US&gl=US&ceid=US:en"
            )
            try:
                entries = parse_rss(url)
            except Exception:
                continue
            for e in entries[: self.max_items_per_ticker]:
                title = e["title"]
                summary = e["summary"]
                text = f"{title} {summary}"
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=title,
                    url=e["link"],
                    summary=summary,
                    tickers=[ticker],
                    event_time=e["published"],
                    signal_strength=0.4,
                    catalyst_hint=_hint_from_text(text),
                    raw={"ticker": ticker},
                )
