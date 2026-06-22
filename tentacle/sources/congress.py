"""Congressional disclosed-trades tentacles (family=flow, Tier 4, FREE, confirm-only).

House and Senate members must disclose stock transactions. The community-run
house-stock-watcher and senate-stock-watcher projects publish the parsed
disclosures as free JSON on S3. A cluster of recent PURCHASES in a name is a soft
confirm signal (flow is confirm-only and can never fire a trade alone). Sales are
ignored: we are long-only and a disclosed sale is not a buy thesis.

This is fuzzy data (disclosures lag the actual trade by up to 45 days), so the
strength is modest and the catalyst hint is left None (it confirms, it does not
define a catalyst).
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..models import RawSignal, SourceFamily, Tier
from ..util import http_json, now_utc, parse_dt
from .base import Source

_HOUSE_URL = (
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/"
    "data/all_transactions.json"
)
_SENATE_URL = (
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/"
    "aggregate/all_transactions.json"
)

# Disclosures are stale by design; only look back a bounded window of recent rows.
_LOOKBACK_DAYS = 45


def _is_purchase(txn_type: str) -> bool:
    t = (txn_type or "").lower()
    return "purchase" in t or t == "buy"


def _recent(date_str: str) -> bool:
    dt = parse_dt(date_str)
    if dt is None:
        return False
    age_days = (now_utc() - dt).total_seconds() / 86400.0
    return 0 <= age_days <= _LOOKBACK_DAYS


class _CongressTrades(Source):
    """Base for a congressional disclosure feed."""

    family = SourceFamily.FLOW.value
    tier = Tier.FLOW.value
    requires_keys: tuple[str, ...] = ()
    min_interval_seconds = 3600  # disclosures change at most daily
    feed_url: str = ""
    chamber: str = ""

    def fetch(self) -> Iterable[RawSignal]:
        try:
            rows = http_json(self.feed_url)
        except Exception:
            return
        if not isinstance(rows, list):
            return
        for row in rows:
            try:
                if not isinstance(row, dict):
                    continue
                ticker = (row.get("ticker") or "").strip().upper()
                # the watchers use "--" / "" for non-equity disclosures
                if not ticker or not ticker.isalpha() or len(ticker) > 5:
                    continue
                txn_type = row.get("type") or row.get("transaction_type") or ""
                if not _is_purchase(txn_type):
                    continue
                date_str = (
                    row.get("transaction_date")
                    or row.get("disclosure_date")
                    or row.get("transaction_date_str")
                    or ""
                )
                if not _recent(date_str):
                    continue
                who = row.get("representative") or row.get("senator") or "member"
                amount = row.get("amount") or ""
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"{self.chamber} purchase: {who} bought {ticker}",
                    url=row.get("ptr_link") or "",
                    summary=f"{who} disclosed a {ticker} purchase ({amount}).",
                    tickers=[ticker],
                    event_time=parse_dt(date_str).isoformat() if parse_dt(date_str) else None,
                    signal_strength=0.5,
                    catalyst_hint=None,
                    raw={"chamber": self.chamber, "amount": amount, "type": txn_type},
                )
            except Exception:
                continue


class HouseTrades(_CongressTrades):
    id = "congress_house"
    chamber = "House"
    feed_url = _HOUSE_URL


class SenateTrades(_CongressTrades):
    id = "congress_senate"
    chamber = "Senate"
    feed_url = _SENATE_URL
