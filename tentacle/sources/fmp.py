"""Financial Modeling Prep (FMP) tentacle (Tier 2).

FMP retired the old /api/v3/ endpoints for new free accounts (they now return a
"legacy endpoint" error), so this uses the current /stable/ API. The valuable free
signal there is the earnings calendar with reported actuals: an EPS actual above
estimate in the last few days is an earnings_beat (family=earnings, can trigger).
News on /stable/ is a paid endpoint, so it is attempted best-effort and skipped
silently when unavailable.

Key-gated: requires FMP_API_KEY.
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..models import Catalyst, RawSignal, SourceFamily, Tier
from ..util import age_seconds, http_json, now_utc
from .base import Source

_STABLE = "https://financialmodelingprep.com/stable"
_RECENT_DAYS = 5  # only emit beats reported within this many days


class FMPEarnings(Source):
    id = "fmp_news"  # kept stable so existing source-state/weights carry over
    family = SourceFamily.EARNINGS.value
    tier = Tier.NEWS_API.value
    requires_keys = ("FMP_API_KEY",)
    min_interval_seconds = 300

    def _key(self) -> str:
        try:
            return self.cfg.integrations.fmp_key or ""
        except Exception:
            return ""

    def fetch(self) -> Iterable[RawSignal]:
        key = self._key()
        if not key:
            return
        try:
            yield from self._earnings(key)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("fmp earnings failed: %s", exc)
        try:
            yield from self._news(key)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("fmp news (likely paid) skipped: %s", exc)

    def _earnings(self, key: str) -> Iterable[RawSignal]:
        from datetime import timedelta
        today = now_utc().date()
        params = {
            "from": (today - timedelta(days=_RECENT_DAYS)).isoformat(),
            "to": (today + timedelta(days=1)).isoformat(),
            "apikey": key,
        }
        data = http_json(f"{_STABLE}/earnings-calendar", params=params)
        if not isinstance(data, list):
            return
        for row in data:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper().strip()
            if not sym or not sym.isalpha():
                continue
            actual = self._num(row.get("epsActual"))
            estimate = self._num(row.get("epsEstimated"))
            if actual is None or estimate is None or actual <= estimate:
                continue
            date = row.get("date")
            # Only a freshly reported beat is a catalyst; skip stale/future rows.
            age_days = age_seconds(date) / 86400.0
            if age_days > _RECENT_DAYS or age_days < -1:
                continue
            denom = abs(estimate) if abs(estimate) > 1e-9 else 1.0
            surprise = (actual - estimate) / denom
            strength = max(0.35, min(0.9, 0.45 + surprise * 2.0))
            yield RawSignal(
                source_id=self.id,
                family=SourceFamily.EARNINGS.value,
                tier=self.tier,
                title=f"{sym} earnings beat: EPS {actual} vs est {estimate}",
                url="https://financialmodelingprep.com/",
                summary=f"Reported EPS {actual} topped {estimate} ({surprise * 100:.1f}%).",
                tickers=[sym],
                event_time=date,
                signal_strength=strength,
                catalyst_hint=Catalyst.EARNINGS_BEAT.value,
                raw={"actual": actual, "estimate": estimate, "surprise_pct": surprise},
            )

    def _news(self, key: str) -> Iterable[RawSignal]:
        # Best-effort: /stable news is a paid endpoint on most plans; returns None on free.
        data = http_json(f"{_STABLE}/news/stock-latest", params={"limit": 50, "apikey": key})
        if not isinstance(data, list):
            return
        for art in data:
            if not isinstance(art, dict):
                continue
            sym = str(art.get("symbol") or "").upper().strip()
            if not sym or not sym.isalpha():
                continue
            yield RawSignal(
                source_id=self.id,
                family=SourceFamily.NEWS.value,
                tier=self.tier,
                title=(art.get("title") or f"News for {sym}").strip(),
                url=(art.get("url") or "").strip(),
                summary=(art.get("text") or "").strip(),
                tickers=[sym],
                event_time=art.get("publishedDate") or art.get("date"),
                signal_strength=0.5,
                catalyst_hint=None,
                raw={"site": art.get("site") or art.get("publisher")},
            )

    @staticmethod
    def _num(v) -> Optional[float]:
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None
