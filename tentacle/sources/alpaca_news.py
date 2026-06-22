"""Alpaca news tentacle (Tier 2, family=news).

Pulls the Alpaca news REST feed (https://data.alpaca.markets/v1beta1/news),
which tags each article with the symbols it concerns. One signal per symbol.

This source is key-gated. The user may set ALPACA_API_KEY or the Alpaca-native
APCA_API_KEY_ID env var. config.Integrations.alpaca_key already coalesces both,
so we override is_enabled to light up whenever that resolved key is present.
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..config import get_config
from ..models import RawSignal, SourceFamily, Tier
from ..util import http_json
from .base import Source


class AlpacaNews(Source):
    id = "alpaca_news"
    family = SourceFamily.NEWS.value
    tier = Tier.NEWS_API.value
    requires_keys = ("ALPACA_API_KEY",)
    min_interval_seconds = 60

    @classmethod
    def is_enabled(cls) -> bool:
        # The user may set APCA_API_KEY_ID instead of ALPACA_API_KEY. The config
        # layer coalesces both into integrations.alpaca_key, so trust that.
        try:
            return bool(get_config().integrations.alpaca_key)
        except Exception:
            return False

    def fetch(self) -> Iterable[RawSignal]:
        try:
            integ = self.cfg.integrations
            key = integ.alpaca_key
            secret = integ.alpaca_secret
            if not key or not secret:
                return
            headers = {
                "APCA-API-KEY-ID": key,
                "APCA-API-SECRET-KEY": secret,
                "accept": "application/json",
            }
            data = http_json(
                "https://data.alpaca.markets/v1beta1/news",
                headers=headers,
                params={"limit": 50, "sort": "desc"},
            )
            if not isinstance(data, dict):
                return
            articles = data.get("news") or []
            if not isinstance(articles, list):
                return
            for art in articles:
                if not isinstance(art, dict):
                    continue
                symbols = art.get("symbols") or []
                if not isinstance(symbols, list) or not symbols:
                    continue
                headline = (art.get("headline") or "").strip()
                summary = (art.get("summary") or "").strip()
                url = (art.get("url") or "").strip()
                event_time = art.get("created_at") or art.get("updated_at")
                for sym in symbols:
                    sym = str(sym).upper().strip()
                    if not sym:
                        continue
                    yield RawSignal(
                        source_id=self.id,
                        family=self.family,
                        tier=self.tier,
                        title=headline or f"News for {sym}",
                        url=url,
                        summary=summary,
                        tickers=[sym],
                        event_time=event_time,
                        signal_strength=0.6,
                        catalyst_hint=None,
                        raw={"source": art.get("source"), "id": art.get("id")},
                    )
        except Exception as exc:  # noqa: BLE001
            self.log.debug("alpaca_news fetch failed: %s", exc)
            return
