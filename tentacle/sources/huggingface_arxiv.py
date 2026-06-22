"""Hugging Face + arXiv research-velocity tentacle (family=dev_ecosystem, Tier 5, FREE).

Two free research-frontier feeds, both confirm-only thematic:
  * HF trending models (api/models?sort=trendingScore) signal where applied ML is going.
  * arXiv recent cs.LG submissions signal where the research frontier is.
We run titles/ids through the theme detector and map detected themes (ai/quantum/
semis) to their proxy ticker baskets. Low strength. Nothing maps -> nothing emitted.
"""
from __future__ import annotations

from typing import Iterable

from ..models import RawSignal, SourceFamily, Tier
from ..util import http_json, now_iso, parse_rss
from .base import Source
from ._watchlist import detect_themes, tickers_for_theme

_HF_URL = "https://huggingface.co/api/models"
_ARXIV_URL = (
    "http://export.arxiv.org/api/query?search_query=cat:cs.LG"
    "&sortBy=submittedDate&sortOrder=descending&max_results=30"
)


def _basket(themes: list[str]) -> list[str]:
    tickers: list[str] = []
    for th in themes:
        for tk in tickers_for_theme(th):
            if tk not in tickers:
                tickers.append(tk)
    return tickers


class HuggingFaceArxiv(Source):
    id = "huggingface_arxiv"
    family = SourceFamily.DEV_ECOSYSTEM.value
    tier = Tier.EXPANDED.value
    requires_keys: tuple[str, ...] = ()
    min_interval_seconds = 3600

    def fetch(self) -> Iterable[RawSignal]:
        yield from self._fetch_hf()
        yield from self._fetch_arxiv()

    def _fetch_hf(self) -> Iterable[RawSignal]:
        try:
            data = http_json(
                _HF_URL,
                params={"sort": "trendingScore", "limit": 30, "full": "false"},
            )
        except Exception:
            return
        if not isinstance(data, list):
            return
        for model in data:
            try:
                if not isinstance(model, dict):
                    continue
                mid = model.get("modelId") or model.get("id") or ""
                tags = " ".join(model.get("tags") or []) if isinstance(model.get("tags"), list) else ""
                themes = detect_themes(f"{mid} {tags}")
                # default ALL trending HF models to the AI basket; it is an AI signal
                if "ai" not in themes:
                    themes = ["ai"] + themes
                tickers = _basket(themes)
                if not tickers:
                    continue
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"HF trending model: {mid}"[:200],
                    url=f"https://huggingface.co/{mid}",
                    summary=f"Trending model; themes: {', '.join(themes)}.",
                    tickers=tickers,
                    event_time=model.get("lastModified") or now_iso(),
                    signal_strength=0.15,
                    catalyst_hint=None,
                    raw={"model": mid, "themes": themes},
                )
            except Exception:
                continue

    def _fetch_arxiv(self) -> Iterable[RawSignal]:
        try:
            entries = parse_rss(_ARXIV_URL)
        except Exception:
            return
        for e in entries:
            try:
                title = e.get("title") or ""
                summary = e.get("summary") or ""
                themes = detect_themes(f"{title} {summary}")
                if not themes:
                    continue
                tickers = _basket(themes)
                if not tickers:
                    continue
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"arXiv cs.LG: {title}"[:200],
                    url=e.get("link") or "",
                    summary=f"themes: {', '.join(themes)}.",
                    tickers=tickers,
                    event_time=e.get("published") or now_iso(),
                    signal_strength=0.12,
                    catalyst_hint=None,
                    raw={"themes": themes},
                )
            except Exception:
                continue
