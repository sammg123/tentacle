"""Shared utilities: logging, HTTP, RSS, time, hashing, ticker extraction.

Deliberately dependency-light. The only third-party imports here are ``requests``
and ``feedparser`` (RSS). Everything else is stdlib so the core engine can run on
a tiny install.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import requests

try:  # feedparser is a core dep but guard so importing util never explodes
    import feedparser  # type: ignore
except Exception:  # pragma: no cover
    feedparser = None  # type: ignore

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
_LOG_FORMAT = "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s"
_configured = False


def setup_logging(level: str | int = "INFO") -> None:
    global _configured
    if _configured:
        return
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=level, format=_LOG_FORMAT, datefmt="%H:%M:%S")
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging(os.environ.get("TENTACLE_LOG_LEVEL", "INFO"))
    return logging.getLogger(name)


log = get_logger("tentacle.util")

# --------------------------------------------------------------------------- #
# Time helpers (all internal timestamps are tz-aware UTC ISO strings)
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value: Any) -> Optional[datetime]:
    """Best-effort parse of a date/time into a tz-aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    s = str(value).strip()
    # feedparser struct_time tuple sometimes passed in
    try:
        from dateutil import parser as _dtp  # type: ignore

        dt = _dtp.parse(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def age_seconds(iso_or_dt: Any) -> float:
    dt = parse_dt(iso_or_dt)
    if dt is None:
        return float("inf")
    return (now_utc() - dt).total_seconds()


# --------------------------------------------------------------------------- #
# Hashing / ids
# --------------------------------------------------------------------------- #
def stable_id(*parts: Any) -> str:
    h = hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
_DEFAULT_TIMEOUT = float(os.environ.get("TENTACLE_HTTP_TIMEOUT", "20"))


def http_get(
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: Optional[float] = None,
    retries: int = 2,
    backoff: float = 1.5,
) -> Optional[requests.Response]:
    """GET with light retry/backoff. Returns the Response or None on failure."""
    hdrs = {"User-Agent": default_user_agent()}
    if headers:
        hdrs.update(headers)
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                url, headers=hdrs, params=params, timeout=timeout or _DEFAULT_TIMEOUT
            )
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                raise requests.HTTPError(f"status {resp.status_code}")
            return resp
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff ** attempt)
    log.debug("GET failed %s: %s", url, last_exc)
    return None


def http_json(url: str, **kwargs) -> Optional[Any]:
    resp = http_get(url, **kwargs)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def http_text(url: str, **kwargs) -> Optional[str]:
    resp = http_get(url, **kwargs)
    return resp.text if resp is not None else None


def default_user_agent() -> str:
    """SEC EDGAR (and good manners) require a contact in the User-Agent."""
    contact = os.environ.get("CONTACT_EMAIL", "goldfedergpt@gmail.com")
    return f"TENTACLE/0.1 ({contact})"


# --------------------------------------------------------------------------- #
# RSS
# --------------------------------------------------------------------------- #
def parse_rss(url: str, *, headers: Optional[dict] = None) -> list[dict]:
    """Fetch + parse an RSS/Atom feed into a list of normalized entry dicts."""
    if feedparser is None:
        log.warning("feedparser not installed; cannot parse %s", url)
        return []
    text = http_text(url, headers=headers)
    if not text:
        return []
    parsed = feedparser.parse(text)
    out: list[dict] = []
    for e in parsed.entries:
        published = (
            e.get("published")
            or e.get("updated")
            or e.get("pubDate")
            or ""
        )
        out.append(
            {
                "title": (e.get("title") or "").strip(),
                "summary": (e.get("summary") or e.get("description") or "").strip(),
                "link": e.get("link") or "",
                "published": published,
                "id": e.get("id") or e.get("guid") or e.get("link") or "",
                "raw": dict(e),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Ticker extraction (cheap heuristic; sources that know the ticker should set it)
# --------------------------------------------------------------------------- #
# Words that look like tickers but never are. Keep this conservative; false
# positives create phantom signals.
_TICKER_STOPWORDS = {
    "CEO", "CFO", "COO", "CTO", "IPO", "ETF", "USA", "USD", "GDP", "FDA", "SEC",
    "NYSE", "FAQ", "API", "AI", "EPS", "Q1", "Q2", "Q3", "Q4", "FY", "PR", "PDT",
    "EST", "EDT", "UTC", "AM", "PM", "INC", "LLC", "LTD", "CO", "THE", "AND",
    "FOR", "NEW", "NOW", "BUY", "ALL", "ITS", "WAS", "ARE", "CAN", "ESG", "GAAP",
    "ETA", "ROI", "YOY", "QOQ", "VS", "OTC", "MNA", "FCC", "DOJ", "FTC", "IRS",
}

_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")
_PAREN_TICKER_RE = re.compile(
    r"\((?:NASDAQ|NYSE|NYSEAMERICAN|NYSE American|AMEX|OTC|OTCMKTS|CBOE)[:\s]+"
    r"([A-Z]{1,5})\)",
    re.IGNORECASE,
)
_BARE_RE = re.compile(r"\b([A-Z]{2,5})\b")


def extract_tickers(text: str, *, aggressive: bool = False) -> list[str]:
    """Pull plausible tickers from free text.

    By default only trusts ``$CASHTAGS`` and ``(NASDAQ: ABCD)`` patterns, which
    are reliable. ``aggressive=True`` also grabs bare uppercase words (noisy,
    only for social/attention sources where it is downweighted anyway).
    """
    if not text:
        return []
    found: list[str] = []
    for m in _PAREN_TICKER_RE.finditer(text):
        found.append(m.group(1).upper())
    for m in _CASHTAG_RE.finditer(text):
        found.append(m.group(1).upper())
    if aggressive:
        for m in _BARE_RE.finditer(text):
            tok = m.group(1).upper()
            if tok not in _TICKER_STOPWORDS:
                found.append(tok)
    # de-dup, drop stopwords, keep order
    seen: set[str] = set()
    out: list[str] = []
    for t in found:
        if t in _TICKER_STOPWORDS or t in seen or not (1 <= len(t) <= 5):
            continue
        seen.add(t)
        out.append(t)
    return out


def chunked(seq: Iterable, n: int):
    buf: list = []
    for item in seq:
        buf.append(item)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf
