"""Unusual Whales options-flow tentacle (family=flow, Tier 4, confirm-only).

Real Unusual Whales flow data (large/sweep options orders, dark-pool prints)
requires a paid API token. There is no reliable free public endpoint for the live
flow firehose, so this tentacle is key-gated: it stays dormant until UW_API_TOKEN
is set and yields nothing otherwise. We do NOT scrape the site (against ToS and
fragile). This keeps the design honest: real flow needs a key.

When a token is present we hit the documented flow-alerts endpoint and emit
confirm-only signals for any clearly mapped ticker. Flow can never fire a trade on
its own (it is in CONFIRM_ONLY_FAMILIES).
"""
from __future__ import annotations

import os
from typing import Iterable

from ..models import RawSignal, SourceFamily, Tier
from ..util import http_json, now_iso
from .base import Source

_FLOW_ALERTS_URL = "https://api.unusualwhales.com/api/option-trades/flow-alerts"


class UnusualWhalesFlow(Source):
    id = "unusual_whales"
    family = SourceFamily.FLOW.value
    tier = Tier.FLOW.value
    # No free firehose. Real flow needs a token; dormant until then.
    requires_keys: tuple[str, ...] = ("UW_API_TOKEN",)
    min_interval_seconds = 120

    def fetch(self) -> Iterable[RawSignal]:
        token = os.environ.get("UW_API_TOKEN", "").strip()
        if not token:
            # Documented honestly: no free endpoint, so nothing to emit.
            return
        try:
            data = http_json(
                _FLOW_ALERTS_URL,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        except Exception:
            return
        if not isinstance(data, dict):
            return
        rows = data.get("data") or data.get("alerts") or []
        if not isinstance(rows, list):
            return
        for row in rows:
            try:
                if not isinstance(row, dict):
                    continue
                ticker = (row.get("ticker") or row.get("underlying_symbol") or "").strip().upper()
                if not ticker or not ticker.isalpha() or len(ticker) > 5:
                    continue
                side = (row.get("type") or row.get("alert_rule") or "").lower()
                # bullish flow (calls / sweeps to the ask) is the only confirm we want
                is_bullish = "call" in side or "bull" in side or "ask" in side
                strength = 0.55 if is_bullish else 0.4
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"Unusual options flow alert: {ticker}",
                    url=row.get("url") or "",
                    summary=str(row.get("alert_rule") or side or "flow alert")[:200],
                    tickers=[ticker],
                    event_time=row.get("created_at") or now_iso(),
                    signal_strength=strength,
                    catalyst_hint=None,
                    raw={"flow": row},
                )
            except Exception:
                continue
