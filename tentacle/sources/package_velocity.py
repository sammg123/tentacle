"""Package-download-velocity tentacle (family=dev_ecosystem, Tier 5, FREE).

Adoption of key AI/data packages is a proxy for the health of the ecosystems that
sell the picks-and-shovels. We poll free download stats from PyPI (pypistats) and
npm and map a small curated package set to themes -> proxy ticker baskets. We emit
a confirm-only signal only when a package's recent weekly downloads clearly exceed
its prior-week baseline (acceleration, not just popularity). Low strength.
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..models import RawSignal, SourceFamily, Tier
from ..util import http_json, now_iso
from .base import Source
from ._watchlist import tickers_for_theme

# package -> theme. Curated, AI/data heavy.
_PYPI_PACKAGES = {
    "vllm": "ai",
    "transformers": "ai",
    "langchain": "ai",
    "torch": "ai",
    "tensorrt": "semis",
}
_NPM_PACKAGES = {
    "ai": "ai",            # the Vercel AI SDK package literally named "ai"
    "@huggingface/inference": "ai",
}

_PYPI_RECENT = "https://pypistats.org/api/packages/{pkg}/recent"
_NPM_RANGE = "https://api.npmjs.org/downloads/range/last-week/{pkg}"


def _pypi_velocity(pkg: str) -> Optional[float]:
    """Return last-week downloads from pypistats, or None."""
    data = http_json(_PYPI_RECENT.format(pkg=pkg))
    if not isinstance(data, dict):
        return None
    d = data.get("data") or {}
    try:
        return float(d.get("last_week") or 0.0)
    except Exception:
        return None


def _npm_accelerating(pkg: str) -> Optional[float]:
    """Return acceleration ratio (last 3 days vs prior 4 days avg) or None."""
    data = http_json(_NPM_RANGE.format(pkg=pkg))
    if not isinstance(data, dict):
        return None
    downloads = data.get("downloads")
    if not isinstance(downloads, list) or len(downloads) < 7:
        return None
    try:
        counts = [int(x.get("downloads", 0)) for x in downloads]
    except Exception:
        return None
    last3 = counts[-3:]
    prior = counts[:-3]
    last3_avg = sum(last3) / len(last3) if last3 else 0.0
    prior_avg = sum(prior) / len(prior) if prior else 0.0
    if prior_avg <= 0:
        return None
    return last3_avg / prior_avg


class PackageVelocity(Source):
    id = "package_velocity"
    family = SourceFamily.DEV_ECOSYSTEM.value
    tier = Tier.EXPANDED.value
    requires_keys: tuple[str, ...] = ()
    min_interval_seconds = 21600  # downloads update at most daily

    def _emit(self, pkg: str, theme: str, note: str, ratio: float) -> Iterable[RawSignal]:
        tickers = tickers_for_theme(theme)
        if not tickers:
            return
        strength = max(0.15, min(0.35, 0.15 + (ratio - 1.2) * 0.2))
        yield RawSignal(
            source_id=self.id,
            family=self.family,
            tier=self.tier,
            title=f"Package velocity: {pkg} ({theme})",
            url="https://pypistats.org/packages/" + pkg,
            summary=note,
            tickers=tickers,
            event_time=now_iso(),
            signal_strength=strength,
            catalyst_hint=None,
            raw={"package": pkg, "theme": theme, "ratio": ratio},
        )

    def fetch(self) -> Iterable[RawSignal]:
        # PyPI: we only have absolute last-week numbers (no clean baseline from this
        # endpoint), so treat sustained high volume as a weak ambient confirm. Honest
        # limitation: this is a level, not an acceleration, for PyPI.
        for pkg, theme in _PYPI_PACKAGES.items():
            try:
                lastweek = _pypi_velocity(pkg)
                if lastweek is None or lastweek <= 0:
                    continue
                # Only the heaviest-adopted packages carry any signal at all.
                if lastweek < 500_000:
                    continue
                note = f"{pkg} ~{int(lastweek):,} PyPI downloads last week."
                yield from self._emit(pkg, theme, note, 1.25)
            except Exception:
                continue
        # npm: we can measure acceleration honestly via the daily range endpoint.
        for pkg, theme in _NPM_PACKAGES.items():
            try:
                ratio = _npm_accelerating(pkg)
                if ratio is None or ratio < 1.3:
                    continue
                note = f"{pkg} npm downloads accelerating {ratio:.2f}x."
                yield from self._emit(pkg, theme, note, ratio)
            except Exception:
                continue
