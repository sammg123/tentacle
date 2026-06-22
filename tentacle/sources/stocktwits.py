"""StockTwits trending tentacle (Tier 3, family=social, FREE).

Reads the public trending-symbols stream. This endpoint is rate-limited; a 429
is swallowed and yields nothing. Social attention is CONFIRMATION ONLY (pump
risk), so catalyst_hint stays None and strength stays modest.
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..models import RawSignal, SourceFamily, Tier
from ..util import http_json
from .base import Source


class StockTwitsTrending(Source):
    id = "stocktwits_trending"
    family = SourceFamily.SOCIAL.value
    tier = Tier.SOCIAL.value
    requires_keys = ()  # free
    min_interval_seconds = 600

    def fetch(self) -> Iterable[RawSignal]:
        try:
            data = http_json(
                "https://api.stocktwits.com/api/2/streams/trending.json"
            )
            # On a 429 or any non-JSON/limit response http_json returns None.
            if not isinstance(data, dict):
                return
            symbols = data.get("symbols") or []
            if not isinstance(symbols, list):
                return
            for s in symbols[:40]:
                if not isinstance(s, dict):
                    continue
                sym = str(s.get("symbol") or "").upper().strip()
                if not sym or not sym.replace(".", "").isalpha():
                    continue
                name = (s.get("title") or "").strip()
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"{sym} trending on StockTwits",
                    url=f"https://stocktwits.com/symbol/{sym}",
                    summary=(
                        f"{name or sym} is trending in social chatter. "
                        "Crowd attention only (confirm)."
                    ),
                    tickers=[sym],
                    event_time=None,
                    signal_strength=0.3,
                    catalyst_hint=None,
                    raw={"watchlist_count": s.get("watchlist_count")},
                )
        except Exception as exc:  # noqa: BLE001
            self.log.debug("stocktwits fetch failed: %s", exc)
            return
