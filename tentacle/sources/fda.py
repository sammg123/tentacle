"""FDA / regulatory tentacles (Tier 1, regulatory family).

Three free regulatory feeds:
  * openFDA drugsfda (https://api.fda.gov/drug/drugsfda.json) recent approvals.
  * FDA press-release RSS.
  * Federal Register documents API (rules/notices that name a company).

HONEST COVERAGE LIMIT: openFDA records carry a SPONSOR NAME, not a stock ticker.
Mapping a sponsor company to a US-listed ticker reliably is hard (subsidiaries,
private firms, foreign listings), so we only yield a signal when a ticker is
directly detectable in the text via a cashtag or an exchange-paren pattern
(e.g. "(NASDAQ: ABCD)"). Records with no detectable ticker are skipped rather
than guessed, to avoid phantom signals. This keeps the feed honest but sparse.
catalyst_hint is always fda_approval here.
"""
from __future__ import annotations

from typing import Iterable

from ..models import Catalyst, RawSignal, SourceFamily, Tier
from ..util import extract_tickers, http_json, parse_rss


from .base import Source


class _FdaBase(Source):
    family = SourceFamily.REGULATORY.value
    tier = Tier.PRIMARY.value
    catalyst_hint = Catalyst.FDA_APPROVAL.value
    min_interval_seconds = 300


class OpenFdaApprovals(_FdaBase):
    """Recent drug approvals from openFDA drugsfda, sorted by submission date.

    Sponsor names rarely include a ticker, so this is usually empty. We still
    poll it because occasionally a record's products text carries a cashtag.
    """

    id = "fda_openfda_drugsfda"

    def fetch(self) -> Iterable[RawSignal]:
        url = "https://api.fda.gov/drug/drugsfda.json"
        params = {
            "search": "submissions.submission_status:AP",
            "sort": "submissions.submission_status_date:desc",
            "limit": 50,
        }
        data = http_json(url, params=params)
        if not isinstance(data, dict):
            return
        for rec in data.get("results", []) or []:
            try:
                sponsor = rec.get("sponsor_name", "") or ""
                products = rec.get("products", []) or []
                names = ", ".join(
                    str(p.get("brand_name") or p.get("active_ingredients", ""))
                    for p in products
                )
                event_time = None
                subs = rec.get("submissions", []) or []
                if subs:
                    event_time = subs[0].get("submission_status_date")
                text = f"{sponsor} {names}"
                tickers = extract_tickers(text)
                if not tickers:
                    continue  # cannot map sponsor -> ticker honestly, skip
                title = f"FDA approval: {sponsor} {names}".strip()
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=title,
                    url="https://www.accessdata.fda.gov/scripts/cder/daf/",
                    summary=names,
                    tickers=tickers,
                    event_time=event_time,
                    signal_strength=0.6,
                    catalyst_hint=self.catalyst_hint,
                    raw={"sponsor": sponsor, "appl_no": rec.get("application_number")},
                )
            except Exception:
                continue


class FdaPressRss(_FdaBase):
    """FDA newsroom press-release RSS. Only yields when a ticker is detectable."""

    id = "fda_press_rss"
    feed_url = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"

    def fetch(self) -> Iterable[RawSignal]:
        for e in parse_rss(self.feed_url):
            title = e["title"]
            summary = e["summary"]
            text = f"{title} {summary}"
            tickers = extract_tickers(text)
            if not tickers:
                continue
            yield RawSignal(
                source_id=self.id,
                family=self.family,
                tier=self.tier,
                title=title,
                url=e["link"],
                summary=summary,
                tickers=tickers,
                event_time=e["published"],
                signal_strength=0.6,
                catalyst_hint=self.catalyst_hint,
                raw={"feed": self.feed_url},
            )


class FederalRegister(_FdaBase):
    """Federal Register documents API. Same ticker-detection-only constraint.

    Most rules/notices name agencies and companies in prose; only those with a
    cashtag or exchange paren get mapped. Sparse by design.
    """

    id = "fda_federal_register"

    def fetch(self) -> Iterable[RawSignal]:
        url = "https://www.federalregister.gov/api/v1/documents.json"
        params = {
            "per_page": 50,
            "order": "newest",
            "conditions[agencies][]": "food-and-drug-administration",
        }
        data = http_json(url, params=params)
        if not isinstance(data, dict):
            return
        for rec in data.get("results", []) or []:
            try:
                title = rec.get("title", "") or ""
                summary = rec.get("abstract", "") or ""
                text = f"{title} {summary}"
                tickers = extract_tickers(text)
                if not tickers:
                    continue
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=title,
                    url=rec.get("html_url", "") or "",
                    summary=summary,
                    tickers=tickers,
                    event_time=rec.get("publication_date"),
                    signal_strength=0.55,
                    catalyst_hint=self.catalyst_hint,
                    raw={"doc_number": rec.get("document_number")},
                )
            except Exception:
                continue
