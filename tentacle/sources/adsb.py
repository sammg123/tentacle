"""Corporate-jet ADS-B tentacle (family=alt, Tier 5, confirm-only, novelty).

Unusual corporate-jet movement near a company HQ can precede M&A or deal activity.
This is a fuzzy, low-weight novelty tentacle. It only activates if the operator has
configured TENTACLE_HQ_COORDS (a JSON map of "TICKER" -> [lat, lon]); without it the
tentacle yields nothing (no way to map a plane to a ticker, so fail safe and stay
quiet). It uses the free adsb.lol API to count aircraft currently near each HQ and
emits a confirm-only signal when the count is unusually high vs a small baseline.

Honest caveats: we cannot tie a generic aircraft to a specific company (private jet
ownership is opaque), so this is presence-near-HQ only and is deliberately the
weakest signal in the system. It must never fire a trade alone (family alt is
confirm-only).
"""
from __future__ import annotations

import json
import os
from typing import Iterable

from ..models import RawSignal, SourceFamily, Tier
from ..util import http_json, now_iso
from .base import Source

# adsb.lol point/radius endpoint: aircraft within <dist> nm of lat/lon.
_ADSB_URL = "https://api.adsb.lol/v2/point/{lat}/{lon}/{dist}"

# A jet count above this near an HQ counts as "unusual" for this crude proxy.
_UNUSUAL_COUNT = 8
_RADIUS_NM = 5


def _load_hq_coords() -> dict[str, list[float]]:
    raw = os.environ.get("TENTACLE_HQ_COORDS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        out: dict[str, list[float]] = {}
        for tk, coords in data.items():
            try:
                lat, lon = float(coords[0]), float(coords[1])
                out[tk.upper()] = [lat, lon]
            except Exception:
                continue
        return out
    except Exception:
        return {}


class AdsbCorporateJets(Source):
    id = "adsb"
    family = SourceFamily.ALT.value
    tier = Tier.EXPANDED.value
    requires_keys: tuple[str, ...] = ()
    min_interval_seconds = 1800

    def fetch(self) -> Iterable[RawSignal]:
        coords = _load_hq_coords()
        if not coords:
            # No HQ map configured: cannot map a plane to a ticker. Fail safe, quiet.
            return
        for ticker, (lat, lon) in coords.items():
            try:
                url = _ADSB_URL.format(lat=lat, lon=lon, dist=_RADIUS_NM)
                data = http_json(url)
                if not isinstance(data, dict):
                    continue
                ac = data.get("ac") or data.get("aircraft") or []
                if not isinstance(ac, list):
                    continue
                count = len(ac)
                if count < _UNUSUAL_COUNT:
                    continue
                strength = max(0.1, min(0.25, 0.1 + (count - _UNUSUAL_COUNT) * 0.01))
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"Unusual air traffic near {ticker} HQ ({count} aircraft)",
                    url="https://adsb.lol/",
                    summary=(f"{count} aircraft within {_RADIUS_NM}nm of {ticker} HQ. "
                             "Fuzzy novelty proxy, presence only."),
                    tickers=[ticker],
                    event_time=now_iso(),
                    signal_strength=strength,
                    catalyst_hint=None,
                    raw={"count": count, "lat": lat, "lon": lon},
                )
            except Exception:
                continue
