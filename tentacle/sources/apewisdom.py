"""ApeWisdom social-mention tentacle (Tier 3, family=social, FREE).

ApeWisdom aggregates Reddit/StockTwits mention counts and ranks across all
stocks. A big positive jump in rank (rank_24h_ago far below the current rank) or
a mention spike is a crowd-attention CONFIRMATION signal. Social is confirm-only
and can never trigger a trade alone (pump risk), so catalyst_hint stays None and
strength is modest.
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..models import RawSignal, SourceFamily, Tier
from ..util import http_json
from .base import Source


class ApeWisdom(Source):
    id = "apewisdom"
    family = SourceFamily.SOCIAL.value
    tier = Tier.SOCIAL.value
    requires_keys = ()  # free
    min_interval_seconds = 600

    def fetch(self) -> Iterable[RawSignal]:
        try:
            data = http_json("https://apewisdom.io/api/v1.0/filter/all-stocks")
            if not isinstance(data, dict):
                return
            results = data.get("results") or []
            if not isinstance(results, list):
                return
            for row in results[:50]:
                if not isinstance(row, dict):
                    continue
                sym = str(row.get("ticker") or "").upper().strip()
                if not sym or not sym.isalpha():
                    continue
                mentions = self._num(row.get("mentions")) or 0.0
                rank = self._num(row.get("rank"))
                rank_prev = self._num(row.get("rank_24h_ago"))
                # A rising rank means a SMALLER number now vs before. Reward jumps.
                rank_delta = 0.0
                if rank is not None and rank_prev is not None:
                    rank_delta = rank_prev - rank  # positive == climbed the board
                # Modest confirm strength: needs a real climb or a mention spike.
                if rank_delta < 5 and mentions < 50:
                    continue
                strength = 0.2
                if rank_delta >= 10:
                    strength += 0.15
                if mentions >= 200:
                    strength += 0.10
                strength = min(0.45, strength)
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"{sym} social mentions rising (rank {rank}, {int(mentions)} mentions)",
                    url="https://apewisdom.io/",
                    summary=(
                        f"ApeWisdom rank moved {rank_delta:+.0f} in 24h with "
                        f"{int(mentions)} mentions. Crowd attention only (confirm)."
                    ),
                    tickers=[sym],
                    event_time=None,
                    signal_strength=strength,
                    catalyst_hint=None,
                    raw={"mentions": mentions, "rank": rank, "rank_24h_ago": rank_prev},
                )
        except Exception as exc:  # noqa: BLE001
            self.log.debug("apewisdom fetch failed: %s", exc)
            return

    @staticmethod
    def _num(v) -> Optional[float]:
        try:
            if v is None:
                return None
            return float(v)
        except (TypeError, ValueError):
            return None
