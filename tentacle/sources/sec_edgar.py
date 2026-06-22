"""SEC EDGAR primary-filing tentacles (Tier 1, highest weight).

You are AT the source here, often ahead of the media. We watch the cross-company
"get current" Atom feeds for material forms and map each filer's CIK to a ticker.

  * 8-K          material events            -> catalyst hint varies (mna / contract)
  * Form 4       insider transactions       -> insider_cluster (cluster buying)
  * SC 13D       activist / >5% stake       -> activist_stake
  * SC 13G       passive >5% stake          -> activist_stake (weaker)

This module is also the canonical example of how to write a tentacle: subclass
Source, implement fetch(), yield RawSignal objects, set a catalyst_hint, swallow
errors. The base class handles dedup, cursor, and reliability weighting.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from ..models import Catalyst, RawSignal, SourceFamily, Tier
from ..util import http_json, parse_rss
from .base import Source

_CIK_MAP_KEY = "sec_cik_ticker_map"
_CIK_TITLE_RE = re.compile(r"\((\d{4,10})\)")


def _load_cik_ticker_map(source: Source) -> dict[str, str]:
    """CIK(int as str, no leading zeros) -> uppercase ticker. Cached in kv."""
    cached = source.storage.kv_get(_CIK_MAP_KEY)
    if isinstance(cached, dict) and cached:
        return cached
    data = http_json("https://www.sec.gov/files/company_tickers.json")
    mapping: dict[str, str] = {}
    if isinstance(data, dict):
        for row in data.values():
            try:
                mapping[str(int(row["cik_str"]))] = row["ticker"].upper()
            except Exception:
                continue
    if mapping:
        source.storage.kv_set(_CIK_MAP_KEY, mapping)
    return mapping


def _cik_from_title(title: str) -> Optional[str]:
    m = _CIK_TITLE_RE.search(title or "")
    if not m:
        return None
    return str(int(m.group(1)))  # strip leading zeros


class _EdgarCurrent(Source):
    """Base for an EDGAR get-current Atom feed of a single form type."""

    family = SourceFamily.SEC_FILING.value
    tier = Tier.PRIMARY.value
    form_type: str = "8-K"
    catalyst_hint: Optional[str] = None
    min_interval_seconds = 120

    def _feed_url(self) -> str:
        return (
            "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
            f"&type={self.form_type}&company=&dateb=&owner=include"
            "&count=100&output=atom"
        )

    def fetch(self) -> Iterable[RawSignal]:
        cikmap = _load_cik_ticker_map(self)
        for e in parse_rss(self._feed_url()):
            title = e["title"]
            cik = _cik_from_title(title)
            ticker = cikmap.get(cik) if cik else None
            if not ticker:
                continue  # only trade names we can map to a US-listed ticker
            yield RawSignal(
                source_id=self.id,
                family=self.family,
                tier=self.tier,
                title=title,
                url=e["link"],
                summary=e["summary"],
                tickers=[ticker],
                event_time=e["published"],
                signal_strength=self._strength(title, e["summary"]),
                catalyst_hint=self._hint(title, e["summary"]),
                raw={"cik": cik, "form": self.form_type},
            )

    def _strength(self, title: str, summary: str) -> float:
        return 0.6

    def _hint(self, title: str, summary: str) -> Optional[str]:
        return self.catalyst_hint


class Edgar8K(_EdgarCurrent):
    id = "sec_8k"
    form_type = "8-K"

    # 8-K item hints in the text -> catalyst category
    def _hint(self, title: str, summary: str) -> Optional[str]:
        text = f"{title} {summary}".lower()
        if any(k in text for k in ("merger", "acquisition", "to be acquired",
                                   "definitive agreement", "tender offer")):
            return Catalyst.MNA_TARGET.value
        if any(k in text for k in ("contract", "award", "purchase order",
                                   "government")):
            return Catalyst.GOV_CONTRACT.value
        if any(k in text for k in ("results of operations", "earnings",
                                   "quarterly results")):
            return Catalyst.EARNINGS_BEAT.value
        if any(k in text for k in ("fda", "approval", "phase 3", "phase iii")):
            return Catalyst.FDA_APPROVAL.value
        return None

    def _strength(self, title: str, summary: str) -> float:
        return 0.65


class EdgarForm4(_EdgarCurrent):
    id = "sec_form4"
    form_type = "4"
    catalyst_hint = Catalyst.INSIDER_CLUSTER.value

    def _strength(self, title: str, summary: str) -> float:
        # purchases matter; the cluster logic lives in the brain (confirmation)
        return 0.5


class Edgar13D(_EdgarCurrent):
    id = "sec_13d"
    form_type = "SC 13D"
    catalyst_hint = Catalyst.ACTIVIST_STAKE.value

    def _strength(self, title: str, summary: str) -> float:
        return 0.7


class Edgar13G(_EdgarCurrent):
    id = "sec_13g"
    form_type = "SC 13G"
    catalyst_hint = Catalyst.ACTIVIST_STAKE.value

    def _strength(self, title: str, summary: str) -> float:
        return 0.45  # passive stake, weaker than 13D
