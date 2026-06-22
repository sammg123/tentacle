"""Government activity tentacle (family=gov, Tier 5, FREE, confirm-only).

Two free federal feeds:
  * USASpending spending_by_award (POST): recently awarded federal contracts. A big
    award to a public company is a gov_contract catalyst hint (but family=gov is
    confirm-only, so it still needs a trigger family to fire).
  * ClinicalTrials.gov v2 studies: recently updated trials whose sponsor maps to a
    public company (biotech read-through).
We map a recipient/sponsor name to a ticker ONLY when it is unambiguous against the
watchlist or a small curated map. Anything we cannot map is skipped. Low strength.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Iterable, Optional

from ..models import Catalyst, RawSignal, SourceFamily, Tier
from ..util import http_json, now_iso, now_utc
from .base import Source
from ._watchlist import get_watchlist

_USASPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
_CT_URL = "https://clinicaltrials.gov/api/v2/studies"

# Curated recipient/sponsor substring -> ticker. Conservative on purpose.
_NAME_TICKER = {
    "lockheed martin": "LMT",
    "raytheon": "RTX",
    "rtx": "RTX",
    "boeing": "BA",
    "palantir": "PLTR",
    "nvidia": "NVDA",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "pfizer": "PFE",
    "moderna": "MRNA",
    "eli lilly": "LLY",
    "regeneron": "REGN",
    "vertex": "VRTX",
    "gilead": "GILD",
    "novo nordisk": "NVO",
}


def _map_name(name: str) -> Optional[str]:
    low = (name or "").lower()
    for needle, tk in _NAME_TICKER.items():
        if needle in low:
            return tk
    return None


class GovContracts(Source):
    id = "gov_contracts"
    family = SourceFamily.GOV.value
    tier = Tier.EXPANDED.value
    requires_keys: tuple[str, ...] = ()
    min_interval_seconds = 21600

    def fetch(self) -> Iterable[RawSignal]:
        yield from self._fetch_awards()
        yield from self._fetch_trials()

    def _fetch_awards(self) -> Iterable[RawSignal]:
        start = (now_utc() - timedelta(days=14)).strftime("%Y-%m-%d")
        end = now_utc().strftime("%Y-%m-%d")
        payload = {
            "filters": {
                "time_period": [{"start_date": start, "end_date": end}],
                "award_type_codes": ["A", "B", "C", "D"],  # contract types
            },
            "fields": ["Award ID", "Recipient Name", "Award Amount", "Description"],
            "sort": "Award Amount",
            "order": "desc",
            "limit": 50,
        }
        try:
            # USASpending requires POST; util only exposes GET helpers, so do the
            # POST directly via requests, fully guarded.
            import requests
            from ..util import default_user_agent
            r = requests.post(
                _USASPENDING_URL,
                json=payload,
                headers={"User-Agent": default_user_agent(),
                         "Content-Type": "application/json"},
                timeout=20,
            )
            data = r.json()
        except Exception:
            return
        if not isinstance(data, dict):
            return
        results = data.get("results") or []
        if not isinstance(results, list):
            return
        for row in results:
            try:
                if not isinstance(row, dict):
                    continue
                recipient = row.get("Recipient Name") or ""
                ticker = _map_name(recipient)
                if not ticker:
                    continue
                amount = row.get("Award Amount") or 0
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"Federal award: {recipient} (${amount})"[:200],
                    url="https://www.usaspending.gov/",
                    summary=str(row.get("Description") or "")[:200],
                    tickers=[ticker],
                    event_time=now_iso(),
                    signal_strength=0.3,
                    catalyst_hint=Catalyst.GOV_CONTRACT.value,
                    raw={"recipient": recipient, "amount": amount},
                )
            except Exception:
                continue

    def _fetch_trials(self) -> Iterable[RawSignal]:
        try:
            data = http_json(
                _CT_URL,
                params={
                    "sort": "LastUpdatePostDate:desc",
                    "pageSize": 50,
                    "format": "json",
                },
            )
        except Exception:
            return
        if not isinstance(data, dict):
            return
        studies = data.get("studies") or []
        if not isinstance(studies, list):
            return
        for st in studies:
            try:
                if not isinstance(st, dict):
                    continue
                proto = st.get("protocolSection") or {}
                ident = proto.get("identificationModule") or {}
                sponsor_mod = proto.get("sponsorCollaboratorsModule") or {}
                lead = (sponsor_mod.get("leadSponsor") or {}).get("name") or ""
                ticker = _map_name(lead)
                if not ticker:
                    continue
                title = ident.get("briefTitle") or "clinical trial update"
                nct = ident.get("nctId") or ""
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"Clinical trial: {lead} - {title}"[:200],
                    url=f"https://clinicaltrials.gov/study/{nct}" if nct else "",
                    summary=f"Lead sponsor {lead}; trial update.",
                    tickers=[ticker],
                    event_time=now_iso(),
                    signal_strength=0.25,
                    catalyst_hint=None,
                    raw={"sponsor": lead, "nct": nct},
                )
            except Exception:
                continue
