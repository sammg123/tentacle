"""Nasdaq trading-halt tentacle (Tier 1, halt family).

The Nasdaq Trader trade-halts RSS lists halts and resumptions in real time, with
the affected symbol embedded in the feed item. A halt is primarily a VOLATILITY
FLAG, not a standalone catalyst: a halt confirms something is happening but says
nothing about direction, so we keep signal_strength low (~0.4) and set no
catalyst_hint (None). It rarely fires alone; it confirms another tentacle's read.
"""
from __future__ import annotations

import re
from typing import Iterable

from ..models import RawSignal, SourceFamily, Tier
from ..util import extract_tickers, parse_rss
from .base import Source

# The feed often carries a structured "Issue Symbol: ABCD" or "<IssueSymbol>".
_SYMBOL_FIELD_RE = re.compile(r"(?:issue\s*symbol|symbol)\s*[:>]?\s*([A-Z]{1,6})",
                              re.IGNORECASE)


class NasdaqHalts(Source):
    id = "halt_nasdaq"
    family = SourceFamily.HALT.value
    tier = Tier.PRIMARY.value
    min_interval_seconds = 60
    feed_url = "http://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"

    def fetch(self) -> Iterable[RawSignal]:
        for e in parse_rss(self.feed_url):
            title = e["title"]
            summary = e["summary"]
            text = f"{title} {summary}"
            ticker = None
            m = _SYMBOL_FIELD_RE.search(text)
            if m:
                cand = m.group(1).upper().strip()
                if 1 <= len(cand) <= 5:
                    ticker = cand
            if not ticker:
                found = extract_tickers(text)
                ticker = found[0] if found else None
            if not ticker:
                continue  # cannot map the halt to a tradeable symbol
            yield RawSignal(
                source_id=self.id,
                family=self.family,
                tier=self.tier,
                title=title or f"Trading halt: {ticker}",
                url=e["link"],
                summary=summary,
                tickers=[ticker],
                event_time=e["published"],
                signal_strength=0.4,
                catalyst_hint=None,  # halts confirm; they rarely trigger alone
                raw={"feed": self.feed_url},
            )
