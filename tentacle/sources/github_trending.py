"""GitHub trending-repos tentacle (family=dev_ecosystem, Tier 5, FREE).

A viral new repo is a leading indicator of where developer mindshare is going. We
query GitHub's search API for highly-starred repos created in the last few days,
run name+description through the theme detector, and map detected themes to their
proxy ticker baskets (a viral AI/quantum repo nudges NVDA/IONQ etc). Confirm-only
thematic, low strength. Unauthenticated GitHub search is rate-limited; on a 403 we
just yield nothing.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Iterable

from ..models import RawSignal, SourceFamily, Tier
from ..util import http_json, now_iso, now_utc
from .base import Source
from ._watchlist import detect_themes, tickers_for_theme

_SEARCH_URL = "https://api.github.com/search/repositories"

_MIN_STARS = 200


class GithubTrending(Source):
    id = "github_trending"
    family = SourceFamily.DEV_ECOSYSTEM.value
    tier = Tier.EXPANDED.value
    requires_keys: tuple[str, ...] = ()
    min_interval_seconds = 3600

    def fetch(self) -> Iterable[RawSignal]:
        since = (now_utc() - timedelta(days=7)).strftime("%Y-%m-%d")
        try:
            data = http_json(
                _SEARCH_URL,
                params={
                    "q": f"created:>{since}",
                    "sort": "stars",
                    "order": "desc",
                    "per_page": 30,
                },
                headers={"Accept": "application/vnd.github+json"},
            )
        except Exception:
            return
        if not isinstance(data, dict):
            return
        items = data.get("items") or []
        if not isinstance(items, list):
            return
        for repo in items:
            try:
                if not isinstance(repo, dict):
                    continue
                name = repo.get("full_name") or repo.get("name") or ""
                desc = repo.get("description") or ""
                stars = int(repo.get("stargazers_count") or 0)
                if stars < _MIN_STARS:
                    continue
                themes = detect_themes(f"{name} {desc}")
                if not themes:
                    continue
                tickers: list[str] = []
                for th in themes:
                    for tk in tickers_for_theme(th):
                        if tk not in tickers:
                            tickers.append(tk)
                if not tickers:
                    continue
                yield RawSignal(
                    source_id=self.id,
                    family=self.family,
                    tier=self.tier,
                    title=f"Trending repo: {name} ({stars} stars)"[:200],
                    url=repo.get("html_url") or "",
                    summary=f"{desc[:160]} | themes: {', '.join(themes)}",
                    tickers=tickers,
                    event_time=repo.get("created_at") or now_iso(),
                    signal_strength=0.18,
                    catalyst_hint=None,
                    raw={"stars": stars, "themes": themes},
                )
            except Exception:
                continue
