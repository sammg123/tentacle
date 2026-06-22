"""LLM backend for the decision-maker. Subscription-first, token-API fallback.

Order of preference (TENTACLE_CLAUDE_BACKEND = auto | cli | api | off, default auto):
  1. the `claude` CLI (Claude Code), which uses your logged-in subscription auth.
     No API tokens spent. Best for low-volume use: the decider only runs on rare,
     confirmation-passing candidates, so this stays well within a subscription.
  2. the Anthropic Messages API over HTTPS, if ANTHROPIC_API_KEY is set.
  3. nothing -> the caller falls back to the deterministic heuristic judge.

Everything is bounded and guarded: a timeout, a single turn of pure reasoning (no
tools), and any failure returns None so trading never blocks on the model.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Optional

from ..config import Config, get_config
from ..util import get_logger

log = get_logger("tentacle.brain.llm")

_TIMEOUT = float(os.environ.get("TENTACLE_CLAUDE_TIMEOUT", "90"))
_API_URL = "https://api.anthropic.com/v1/messages"
_API_MODEL = os.environ.get("TENTACLE_CLAUDE_MODEL", "claude-sonnet-4-6")
_API_MAX_TOKENS = int(os.environ.get("TENTACLE_CLAUDE_MAX_TOKENS", "700"))


def _backend(cfg: Config) -> str:
    return (os.environ.get("TENTACLE_CLAUDE_BACKEND", "auto") or "auto").strip().lower()


def claude_available(cfg: Optional[Config] = None) -> str:
    """Return which backend would be used right now: 'cli' | 'api' | 'none'."""
    cfg = cfg or get_config()
    mode = _backend(cfg)
    if mode == "off":
        return "none"
    has_cli = shutil.which("claude") is not None
    has_api = bool(getattr(cfg.integrations, "anthropic_key", ""))
    if mode == "cli":
        return "cli" if has_cli else "none"
    if mode == "api":
        return "api" if has_api else "none"
    # auto: prefer the subscription CLI, then the token API
    if has_cli:
        return "cli"
    if has_api:
        return "api"
    return "none"


def _under_daily_cap() -> bool:
    """Hard daily ceiling on LLM calls (kid + mother + A/B), for predictable cost.
    TENTACLE_LLM_DAILY_CALL_CAP <= 0 (default) means unlimited. When the cap is hit,
    claude_complete returns None and the caller falls back to the free heuristic."""
    try:
        cap = int(os.environ.get("TENTACLE_LLM_DAILY_CALL_CAP", "0") or 0)
    except ValueError:
        cap = 0
    if cap <= 0:
        return True
    try:
        from ..storage import get_storage
        from ..util import now_utc
        st = get_storage()
        key = f"llm.calls.{now_utc().date().isoformat()}"
        n = int(st.kv_get(key, 0) or 0)
        if n >= cap:
            return False
        st.kv_set(key, n + 1)
        return True
    except Exception:  # noqa: BLE001 (never let the cap check block on storage issues)
        return True


def claude_complete(system: str, user: str, cfg: Optional[Config] = None,
                    timeout: Optional[float] = None,
                    model: Optional[str] = None) -> Optional[str]:
    """Return the model's text response, or None on any failure.

    ``model`` selects the role's model (kid=Sonnet, mother=Opus); when omitted it
    falls back to TENTACLE_CLAUDE_MODEL (CLI) / the API default.
    """
    cfg = cfg or get_config()
    backend = claude_available(cfg)
    if backend == "none":
        return None
    # Only count real LLM calls against the daily cap (heuristic mode is free).
    if not _under_daily_cap():
        log.info("LLM daily call cap reached; falling back to the heuristic")
        return None
    if backend == "cli":
        out = _via_cli(system, user, timeout or _TIMEOUT, model)
        if out:
            return out
        # auto-mode courtesy: if the CLI failed but an API key exists, try it
        if _backend(cfg) == "auto" and getattr(cfg.integrations, "anthropic_key", ""):
            return _via_api(system, user, cfg.integrations.anthropic_key,
                            timeout or _TIMEOUT, model)
        return None
    if backend == "api":
        return _via_api(system, user, cfg.integrations.anthropic_key,
                        timeout or _TIMEOUT, model)
    return None


# --------------------------------------------------------------------------- #
# Subscription path: shell out to the `claude` CLI (Claude Code)
# --------------------------------------------------------------------------- #
def _via_cli(system: str, user: str, timeout: float,
             model: Optional[str] = None) -> Optional[str]:
    exe = shutil.which("claude")
    if not exe:
        return None
    cmd = [exe, "-p", "--output-format", "json", "--system-prompt", system]
    model = model or os.environ.get("TENTACLE_CLAUDE_MODEL")
    if model:
        cmd += ["--model", model]
    # Pure reasoning task: forbid every tool so it cannot read files, hang on a
    # permission prompt, or wander. The prompt itself also says "data, not commands".
    cmd += ["--disallowed-tools", "Bash", "Read", "Edit", "Write", "WebFetch",
            "WebSearch", "Glob", "Grep"]
    try:
        proc = subprocess.run(
            cmd, input=user, text=True, capture_output=True, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 (timeout, missing binary, etc.)
        log.debug("claude CLI call failed: %s", exc)
        return None
    if proc.returncode != 0:
        log.debug("claude CLI returned %d: %s", proc.returncode, (proc.stderr or "")[:200])
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    # --output-format json wraps the answer: {"type":"result","result":"<text>",...}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            if obj.get("is_error"):
                return None
            res = obj.get("result")
            if isinstance(res, str) and res.strip():
                return res.strip()
    except Exception:
        pass
    return raw  # fall back to treating stdout as the text itself


# --------------------------------------------------------------------------- #
# Token path: Anthropic Messages API over HTTPS
# --------------------------------------------------------------------------- #
def _via_api(system: str, user: str, api_key: str, timeout: float,
             model: Optional[str] = None) -> Optional[str]:
    if not api_key:
        return None
    try:
        import requests
    except Exception:
        return None
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": model or _API_MODEL,
        "max_tokens": _API_MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    try:
        resp = requests.post(_API_URL, headers=headers, data=json.dumps(body), timeout=timeout)
    except Exception:
        return None
    if resp is None or resp.status_code != 200:
        log.debug("anthropic API status %s", getattr(resp, "status_code", "n/a"))
        return None
    try:
        data = resp.json()
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict) and b.get("type") == "text")
        return text.strip() or None
    except Exception:
        return None
