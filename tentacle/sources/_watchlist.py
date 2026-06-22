"""Shared watchlist + theme->ticker map for per-ticker and thematic tentacles.

Per-ticker sources (Google News, Finnhub, StockTwits, Reddit, Wikipedia, Trends)
should iterate a bounded watchlist to stay polite. Thematic confirm-only sources
(GitHub/PyPI/HN/HF/arXiv/ADS-B) map a detected theme to a small basket of liquid
proxy tickers (e.g. an AI repo going viral -> NVDA/SMCI/AMD).
"""
from __future__ import annotations

import os
from typing import Optional

# A compact basket of liquid, optionable, news-sensitive names across sectors.
DEFAULT_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "AMZN", "GOOGL", "META", "AVGO",
    "SMCI", "PLTR", "COIN", "MSTR", "MARA", "RIOT", "SOFI", "AFRM", "RIVN",
    "LCID", "NIO", "INTC", "MU", "QCOM", "ARM", "DELL", "CRM", "NFLX", "BA",
    "LMT", "RTX", "PFE", "MRNA", "LLY", "NVO", "REGN", "VRTX", "GILD", "AMC",
    "GME", "BBAI", "IONQ", "RGTI", "QBTS", "QUBT", "ASTS", "RKLB", "JOBY",
]

# Theme -> proxy tickers for confirm-only thematic sources.
THEME_TICKERS: dict[str, list[str]] = {
    "ai": ["NVDA", "AMD", "SMCI", "AVGO", "PLTR", "MSFT", "GOOGL", "META"],
    "quantum": ["IONQ", "RGTI", "QBTS", "QUBT"],
    "semis": ["NVDA", "AMD", "MU", "AVGO", "ARM", "QCOM", "INTC", "SMCI"],
    "crypto": ["COIN", "MSTR", "MARA", "RIOT", "HOOD"],
    "space": ["RKLB", "ASTS", "LUNR", "JOBY"],
    "biotech": ["MRNA", "PFE", "LLY", "NVO", "REGN", "VRTX", "GILD"],
    "ev": ["TSLA", "RIVN", "LCID", "NIO"],
    "defense": ["LMT", "RTX", "BA", "BBAI", "PLTR"],
}


def get_watchlist(storage=None, limit: int = 60) -> list[str]:
    env = os.environ.get("TENTACLE_WATCHLIST", "").strip()
    out: list[str] = []
    if env:
        out = [t.strip().upper() for t in env.split(",") if t.strip()]
    else:
        out = list(DEFAULT_WATCHLIST)
    # fold in tickers we have recently seen signals on (keeps the net wide)
    if storage is not None:
        try:
            from ..util import now_utc
            from datetime import timedelta
            since = (now_utc() - timedelta(days=3)).isoformat()
            seen: list[str] = []
            for row in storage.recent_signals(since):
                import json
                for t in json.loads(row.get("tickers") or "[]"):
                    if t and t not in out and t not in seen:
                        seen.append(t)
            out = out + seen
        except Exception:
            pass
    # de-dup, cap
    final: list[str] = []
    for t in out:
        if t and t not in final:
            final.append(t)
        if len(final) >= limit:
            break
    return final


def tickers_for_theme(theme: str) -> list[str]:
    return THEME_TICKERS.get(theme.lower(), [])


def detect_themes(text: str) -> list[str]:
    t = (text or "").lower()
    themes: list[str] = []
    kw = {
        "ai": ["ai ", "artificial intelligence", "llm", "gpt", "model", "neural",
               "machine learning", "inference", "transformer"],
        "quantum": ["quantum"],
        "semis": ["chip", "semiconductor", "gpu", "wafer", "foundry"],
        "crypto": ["bitcoin", "crypto", "ethereum", "blockchain"],
        "space": ["satellite", "rocket", "space", "launch"],
        "biotech": ["fda", "phase 3", "clinical", "drug", "therapy"],
        "ev": ["electric vehicle", " ev ", "battery"],
        "defense": ["defense", "missile", "military", "pentagon"],
    }
    for theme, words in kw.items():
        if any(w in t for w in words):
            themes.append(theme)
    return themes
