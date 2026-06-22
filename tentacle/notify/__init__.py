"""Notifications. Always logs; optionally pings Telegram and/or email when keys
are present. A notification failure must never break the trading loop."""
from __future__ import annotations

from typing import Optional

from ..config import Config, get_config
from ..util import get_logger, http_get

log = get_logger("tentacle.notify")


def dispatch(message: str, cfg: Optional[Config] = None) -> None:
    cfg = cfg or get_config()
    log.info("NOTIFY: %s", message)
    _telegram(message, cfg)


def _telegram(message: str, cfg: Config) -> None:
    token = cfg.integrations.telegram_token
    chat_id = cfg.integrations.telegram_chat_id
    if not (token and chat_id):
        return
    try:
        http_get(
            f"https://api.telegram.org/bot{token}/sendMessage",
            params={"chat_id": chat_id, "text": f"\U0001f419 {message}"},
            retries=0,
        )
    except Exception:  # noqa: BLE001
        pass
