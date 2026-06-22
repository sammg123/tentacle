"""Finnhub earnings tentacle (Tier 2, family=earnings).

Polls Finnhub for earnings surprises over the bounded watchlist. A positive
surprise (actual > estimate) emits an earnings_beat catalyst with strength scaled
by the surprise percentage. Optionally folds in recommendation trends as a mild
strength bump (analyst upgrades), but the catalyst remains earnings-driven.

Key-gated: requires FINNHUB_API_KEY.
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..models import Catalyst, RawSignal, SourceFamily, Tier
from ..util import http_json
from .base import Source
from ._watchlist import get_watchlist


class FinnhubEarnings(Source):
    id = "finnhub_earnings"
    family = SourceFamily.EARNINGS.value
    tier = Tier.NEWS_API.value
    requires_keys = ("FINNHUB_API_KEY",)
    min_interval_seconds = 120

    def _key(self) -> str:
        try:
            return self.cfg.integrations.finnhub_key or ""
        except Exception:
            return ""

    def fetch(self) -> Iterable[RawSignal]:
        key = self._key()
        if not key:
            return
        try:
            tickers = get_watchlist(self.storage)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("watchlist failed: %s", exc)
            return
        for sym in tickers:
            try:
                yield from self._for_symbol(sym, key)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("finnhub %s failed: %s", sym, exc)
                continue

    def _for_symbol(self, sym: str, key: str) -> Iterable[RawSignal]:
        data = http_json(
            "https://finnhub.io/api/v1/stock/earnings",
            params={"symbol": sym, "token": key},
        )
        if not isinstance(data, list) or not data:
            return
        # Most recent reported quarter first.
        latest: Optional[dict] = None
        for row in data:
            if not isinstance(row, dict):
                continue
            if row.get("actual") is None:
                continue
            latest = row
            break
        if latest is None:
            return
        actual = self._num(latest.get("actual"))
        estimate = self._num(latest.get("estimate"))
        if actual is None or estimate is None:
            return
        # Only fire on a beat. A miss is not a long-only catalyst.
        if actual <= estimate:
            return
        denom = abs(estimate) if abs(estimate) > 1e-9 else 1.0
        surprise_pct = (actual - estimate) / denom
        # Scale strength: small beat ~0.4, big beat saturates near 0.9.
        strength = max(0.3, min(0.9, 0.4 + surprise_pct * 2.0))
        period = latest.get("period") or ""
        yield RawSignal(
            source_id=self.id,
            family=self.family,
            tier=self.tier,
            title=f"{sym} earnings beat ({period}): actual {actual} vs est {estimate}",
            url=f"https://finnhub.io/",
            summary=(
                f"Reported EPS {actual} topped the {estimate} estimate, a "
                f"{surprise_pct * 100:.1f}% surprise."
            ),
            tickers=[sym],
            event_time=period or None,
            signal_strength=strength,
            catalyst_hint=Catalyst.EARNINGS_BEAT.value,
            raw={
                "actual": actual,
                "estimate": estimate,
                "surprise_pct": surprise_pct,
                "period": period,
            },
        )

    @staticmethod
    def _num(v) -> Optional[float]:
        try:
            if v is None:
                return None
            return float(v)
        except (TypeError, ValueError):
            return None
