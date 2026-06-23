"""Optional Supabase mirror: push the bot's decisions + trades to a Supabase project so they
surface on the read-only Bloomberg terminal (see TERMINAL.md).

OFF unless `cfg.integrations.supabase_url` AND `supabase_key` are set. Writes to PREFIXED tables
(default `tentacle_*`) so they never collide with another bot sharing the same project. The push
is:
  * idempotent: it UPSERTs on the bot's own primary keys (decisions.candidate_id, trades.id), so
    re-sending the recent window every cycle is safe and self-healing;
  * bounded: a retention cap prunes rows older than RETENTION_DAYS, so the mirror can never grow
    unbounded no matter how long the bot runs;
  * fully guarded: any failure is swallowed, so a mirror hiccup never breaks the trading loop.

This is one-way and write-only (the terminal only READS these tables). It never reads back into the
bot and never influences a decision.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from .config import Config, get_config
from .storage import Storage, get_storage
from .util import get_logger, now_iso, now_utc

log = get_logger("tentacle.mirror")

RETENTION_DAYS = 90
_DECISION_PUSH = 200          # re-upsert the most recent N decisions each sync (idempotent)
_TRADE_PUSH = 200
_PRUNE_EVERY_HOURS = 24


def _creds(cfg: Config):
    i = cfg.integrations
    key = getattr(i, "supabase_key", "") or ""
    if not key:
        return None
    # SUPABASE_URL is optional: the service-role key is a JWT whose payload carries the project
    # ref, so we can derive the URL from the key alone.
    url = (getattr(i, "supabase_url", "") or "").rstrip("/") or _url_from_key(key)
    if not url:
        return None
    return url, key, (getattr(i, "supabase_prefix", "") or "")


def _url_from_key(key: str) -> str:
    """Derive https://<ref>.supabase.co from a Supabase JWT (its payload has the project ref)."""
    try:
        import base64
        import json
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)            # pad to a multiple of 4
        ref = json.loads(base64.urlsafe_b64decode(payload.encode())).get("ref")
        return f"https://{ref}.supabase.co" if ref else ""
    except Exception:  # noqa: BLE001
        return ""


def _headers(key: str, *, upsert: bool) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal" if upsert else "return=minimal",
    }


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None      # drop NaN


def _decision_row(d: dict) -> dict:
    # decisions -> tentacle_signals_log (the kid's journal: BUY/PASS + conviction + thesis)
    return {
        "candidate_id": d.get("candidate_id"),
        "ts": d.get("decided_at"),
        "symbol": d.get("ticker"),
        "state": d.get("catalyst"),
        "action": d.get("verdict"),
        "conviction": _num(d.get("conviction")),
        "suggested_size_pct": None,           # the kid sets conviction; the engine owns size
        "price_at_signal": _num(d.get("entry_price")),
        "rationale": d.get("thesis"),
    }


def _trade_row(t: dict) -> dict:
    # trades -> tentacle_executions (the actual paper entries/exits)
    return {
        "trade_id": t.get("id"),
        "broker_order_id": t.get("broker_order_id"),
        "symbol": t.get("ticker"),
        "side": "buy",                        # long-only: positions open on the buy
        "intent_notional": _num(t.get("size_dollars")),
        "intent_qty": _num(t.get("qty")),
        "suggested_price": _num(t.get("entry_price")),
        "status": t.get("status"),
        "filled_qty": _num(t.get("qty")),
        "filled_avg_price": _num(t.get("entry_price")),
        "mode": "paper" if t.get("paper") else "live",
        "note": t.get("catalyst"),
        "submitted_at": t.get("entry_time"),
        "updated_at": t.get("exit_time") or t.get("entry_time"),
    }


def _upsert(url: str, key: str, table: str, rows: list, on_conflict: str) -> None:
    if not rows:
        return
    import requests
    requests.post(
        f"{url}/rest/v1/{table}",
        params={"on_conflict": on_conflict},
        json=rows,
        headers=_headers(key, upsert=True),
        timeout=20,
    )


def sync(storage: Optional[Storage] = None, cfg: Optional[Config] = None) -> None:
    """Push the recent decisions + trades to Supabase. No-op without creds. Never raises."""
    cfg = cfg or get_config()
    creds = _creds(cfg)
    if creds is None:
        return
    url, key, prefix = creds
    storage = storage or get_storage()
    try:
        decs = [_decision_row(d) for d in storage.recent_decisions(limit=_DECISION_PUSH)]
        decs = [r for r in decs if r.get("candidate_id")]
        _upsert(url, key, f"{prefix}signals_log", decs, "candidate_id")

        trades = list(storage.open_trades()) + list(storage.closed_trades())[-_TRADE_PUSH:]
        # dedupe by trade id (open + closed can't overlap, but be safe)
        rows = {t.get("id"): _trade_row(t) for t in trades if t.get("id")}
        _upsert(url, key, f"{prefix}executions", list(rows.values()), "trade_id")

        # the mother's coaching: push the current champion policy lessons for the MOTHER tab
        try:
            from .policy import get_champion_policy
            pol = get_champion_policy(storage)
            _upsert(url, key, f"{prefix}mother", [{
                "id": "current", "ts": now_iso(),
                "policy_version": int(pol.get("version", 1) or 1),
                "lessons": list(pol.get("lessons") or []),
                "changelog": pol.get("changelog") or "",
            }], "id")
        except Exception as exc:  # noqa: BLE001
            log.debug("mirror mother-policy push failed: %s", exc)

        _maybe_prune(storage, url, key, prefix)
    except Exception as exc:  # noqa: BLE001 (a mirror failure must never break the loop)
        log.debug("supabase mirror sync failed: %s", exc)


def _maybe_prune(storage: Storage, url: str, key: str, prefix: str) -> None:
    """Delete rows older than the retention window, at most once per _PRUNE_EVERY_HOURS."""
    now_ts = now_utc().timestamp()
    try:
        last = float(storage.kv_get("mirror.last_prune_ts", 0) or 0)
    except (TypeError, ValueError):
        last = 0.0
    if last and now_ts - last < _PRUNE_EVERY_HOURS * 3600:
        return
    cutoff = (now_utc() - timedelta(days=RETENTION_DAYS)).isoformat()
    try:
        import requests
        h = _headers(key, upsert=False)
        # PostgREST requires a filter on DELETE; the cutoff filter is that filter.
        requests.delete(f"{url}/rest/v1/{prefix}signals_log",
                        params={"ts": f"lt.{cutoff}"}, headers=h, timeout=20)
        requests.delete(f"{url}/rest/v1/{prefix}executions",
                        params={"submitted_at": f"lt.{cutoff}"}, headers=h, timeout=20)
    except Exception as exc:  # noqa: BLE001
        log.debug("supabase mirror prune failed: %s", exc)
    storage.kv_set("mirror.last_prune_ts", now_ts)
