"""Alpha Vantage news + sentiment tentacle (Tier 2, family=news).

Uses the NEWS_SENTIMENT endpoint, which returns articles each tagged with a list
of ticker_sentiment entries (per-ticker relevance + sentiment score). We emit one
signal per relevant ticker, with strength derived from the sentiment score and
relevance so noise is downweighted.

Key-gated: requires ALPHAVANTAGE_API_KEY.
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..models import RawSignal, SourceFamily, Tier
from ..util import http_json
from .base import Source


class AlphaVantageNews(Source):
    id = "alphavantage_news"
    family = SourceFamily.NEWS.value
    tier = Tier.NEWS_API.value
    requires_keys = ("ALPHAVANTAGE_API_KEY",)
    min_interval_seconds = 300

    def _key(self) -> str:
        try:
            return self.cfg.integrations.alphavantage_key or ""
        except Exception:
            return ""

    def fetch(self) -> Iterable[RawSignal]:
        key = self._key()
        if not key:
            return
        try:
            data = http_json(
                "https://www.alphavantage.co/query",
                params={
                    "function": "NEWS_SENTIMENT",
                    "sort": "LATEST",
                    "limit": 50,
                    "apikey": key,
                },
            )
            if not isinstance(data, dict):
                return
            feed = data.get("feed") or []
            if not isinstance(feed, list):
                return
            for art in feed:
                if not isinstance(art, dict):
                    continue
                title = (art.get("title") or "").strip()
                url = (art.get("url") or "").strip()
                summary = (art.get("summary") or "").strip()
                event_time = art.get("time_published")
                ts_list = art.get("ticker_sentiment") or []
                if not isinstance(ts_list, list):
                    continue
                for ts in ts_list:
                    if not isinstance(ts, dict):
                        continue
                    sym = str(ts.get("ticker") or "").upper().strip()
                    if not sym or not sym.isalpha():
                        continue
                    score = self._num(ts.get("ticker_sentiment_score"))
                    relevance = self._num(ts.get("relevance_score"))
                    if score is None:
                        continue
                    # Only positive sentiment is a long catalyst confirmation.
                    if score <= 0.10:
                        continue
                    rel = relevance if relevance is not None else 0.5
                    # Map sentiment (~ -1..1) and relevance (0..1) to 0..1 strength.
                    strength = max(0.2, min(0.85, (score * 0.6 + 0.3) * (0.5 + rel * 0.5)))
                    yield RawSignal(
                        source_id=self.id,
                        family=self.family,
                        tier=self.tier,
                        title=title or f"News for {sym}",
                        url=url,
                        summary=summary,
                        tickers=[sym],
                        event_time=event_time,
                        signal_strength=strength,
                        catalyst_hint=None,
                        raw={"sentiment": score, "relevance": rel},
                    )
        except Exception as exc:  # noqa: BLE001
            self.log.debug("alphavantage fetch failed: %s", exc)
            return

    @staticmethod
    def _num(v) -> Optional[float]:
        try:
            if v is None:
                return None
            return float(v)
        except (TypeError, ValueError):
            return None
