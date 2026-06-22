"""Reddit social tentacle (Tier 3, family=social, confirmation only).

Counts repeated ticker mentions across hot posts in r/wallstreetbets + r/stocks.
Social attention is CONFIRMATION ONLY (pump risk): catalyst_hint stays None and
strength stays modest.

Two access modes:
  * If REDDIT_CLIENT_ID + REDDIT_CLIENT_SECRET are set, it uses Reddit's
    application-only OAuth (client_credentials grant) against oauth.reddit.com.
    This is REQUIRED in the cloud: Reddit blocks unauthenticated .json access from
    datacenter IPs (Railway), so without a key this tentacle goes silent there.
  * Otherwise it falls back to the public .json endpoint (works from a home IP).

One Reddit "script" app's client_id/secret works here; you can reuse an app you
already created, you do not need a new one.
"""
from __future__ import annotations

import time
from typing import Iterable, Optional

from ..models import RawSignal, SourceFamily, Tier
from ..util import default_user_agent, extract_tickers, http_json
from .base import Source

_SUBREDDITS = ("wallstreetbets", "stocks")
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"


class RedditWSB(Source):
    id = "reddit_wsb"
    family = SourceFamily.SOCIAL.value
    tier = Tier.SOCIAL.value
    requires_keys = ()  # always live; uses OAuth when keyed, public JSON otherwise
    min_interval_seconds = 600

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._token: Optional[str] = None
        self._token_exp: float = 0.0

    # ------------------------------------------------------------------ oauth
    def _creds(self):
        try:
            i = self.cfg.integrations
            return (i.reddit_client_id or "", i.reddit_secret or "")
        except Exception:
            return ("", "")

    def _bearer(self) -> Optional[str]:
        cid, secret = self._creds()
        if not (cid and secret):
            return None
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        try:
            import requests
            resp = requests.post(
                _TOKEN_URL,
                auth=(cid, secret),
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": default_user_agent()},
                timeout=15,
            )
            if resp.status_code != 200:
                self.log.debug("reddit token http %s: %s", resp.status_code, resp.text[:120])
                return None
            j = resp.json()
            self._token = j.get("access_token")
            self._token_exp = time.time() + float(j.get("expires_in", 3600))
            return self._token
        except Exception as exc:  # noqa: BLE001
            self.log.debug("reddit token failed: %s", exc)
            return None

    # ------------------------------------------------------------------ fetch
    def fetch(self) -> Iterable[RawSignal]:
        bearer = self._bearer()
        counts: dict[str, int] = {}
        example: dict[str, dict] = {}
        for sub in _SUBREDDITS:
            try:
                self._scan(sub, counts, example, bearer)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("reddit %s failed: %s", sub, exc)
                continue
        for sym, n in counts.items():
            if n < 2:  # require repetition to filter one-off noise
                continue
            strength = min(0.45, 0.2 + 0.05 * (n - 2))
            meta = example.get(sym, {})
            yield RawSignal(
                source_id=self.id,
                family=self.family,
                tier=self.tier,
                title=f"{sym} mentioned {n}x in Reddit hot posts",
                url=meta.get("url", "https://www.reddit.com/r/wallstreetbets/"),
                summary=f"{sym} appeared in {n} hot posts. Crowd attention only (confirm).",
                tickers=[sym],
                event_time=None,
                signal_strength=strength,
                catalyst_hint=None,
                raw={"mentions": n, "subreddit": meta.get("sub"),
                     "auth": "oauth" if bearer else "public"},
            )

    def _scan(self, sub: str, counts: dict, example: dict, bearer: Optional[str]) -> None:
        if bearer:
            url = f"https://oauth.reddit.com/r/{sub}/hot"
            headers = {"User-Agent": default_user_agent(),
                       "Authorization": f"bearer {bearer}"}
        else:
            url = f"https://www.reddit.com/r/{sub}/hot.json"
            headers = {"User-Agent": default_user_agent()}
        data = http_json(url, headers=headers, params={"limit": 50})
        if not isinstance(data, dict):
            return
        children = (data.get("data") or {}).get("children") or []
        if not isinstance(children, list):
            return
        for ch in children:
            if not isinstance(ch, dict):
                continue
            post = ch.get("data") or {}
            if not isinstance(post, dict):
                continue
            text = f"{post.get('title', '')} {post.get('selftext', '')}"
            for sym in extract_tickers(text, aggressive=True):
                counts[sym] = counts.get(sym, 0) + 1
                if sym not in example:
                    permalink = post.get("permalink") or ""
                    example[sym] = {
                        "url": f"https://www.reddit.com{permalink}" if permalink else "",
                        "sub": sub,
                    }
