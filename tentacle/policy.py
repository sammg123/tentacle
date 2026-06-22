"""The kid's decision POLICY: the only thing the mother is allowed to edit.

A policy is the kid's reasoning guidance, nothing more:
    {"version": int, "system_prompt": str|None, "lessons": [str],
     "created_by": "seed"|"mother", "created_at": str, "changelog": str}

`system_prompt=None` means "use the built-in decider system prompt". `lessons` are
short imperative reminders the mother appends to teach the kid (e.g. "Do not trust a
single social spike with no primary filing"). The effective prompt the kid sees is
the base prompt plus the lessons.

HARD BOUNDARY: a policy can only shape REASONING. It can never set position size,
stops, the ruin floor, the daily halt, the ladder caps, or the settled-cash rule.
Those live in config.py + risk/ and are off-limits to the mother by construction
(she has no API to them).
"""
from __future__ import annotations

from typing import Optional

from .storage import Storage, get_storage
from .util import get_logger, now_iso

log = get_logger("tentacle.policy")

K_CHAMPION = "policy.champion"
K_CHALLENGER = "policy.challenger"
K_LAST_PROMO = "policy.last_promotion_at"


def _seed() -> dict:
    return {"version": 1, "system_prompt": None, "lessons": [],
            "created_by": "seed", "created_at": now_iso(), "changelog": "initial seed"}


def get_champion_policy(storage: Optional[Storage] = None) -> dict:
    storage = storage or get_storage()
    pol = storage.kv_get(K_CHAMPION)
    if not isinstance(pol, dict):
        pol = _seed()
        storage.kv_set(K_CHAMPION, pol)
    return pol


def get_challenger_policy(storage: Optional[Storage] = None) -> Optional[dict]:
    storage = storage or get_storage()
    pol = storage.kv_get(K_CHALLENGER)
    return pol if isinstance(pol, dict) else None


def set_challenger(storage: Optional[Storage], lessons: list[str],
                   changelog: str = "", system_prompt: Optional[str] = None) -> dict:
    storage = storage or get_storage()
    champ = get_champion_policy(storage)
    challenger = {
        "version": int(champ.get("version", 1)) + 1,
        "system_prompt": system_prompt,         # usually None: only lessons change
        "lessons": list(lessons or []),
        "created_by": "mother",
        "created_at": now_iso(),
        "changelog": changelog or "",
    }
    storage.kv_set(K_CHALLENGER, challenger)
    log.info("mother proposed challenger v%d: %s", challenger["version"], changelog)
    return challenger


def promote_challenger(storage: Optional[Storage] = None) -> Optional[dict]:
    """Promote the stored challenger to champion. Returns the new champion or None."""
    storage = storage or get_storage()
    challenger = get_challenger_policy(storage)
    if not challenger:
        return None
    storage.kv_set(K_CHAMPION, challenger)
    storage.kv_set(K_CHALLENGER, None)
    storage.kv_set(K_LAST_PROMO, now_iso())
    log.info("mother PROMOTED policy v%d to champion", challenger.get("version"))
    return challenger


def policy_version(storage: Optional[Storage] = None) -> int:
    return int(get_champion_policy(storage).get("version", 1))


def effective_system_prompt(base_prompt: str, storage: Optional[Storage] = None,
                            policy: Optional[dict] = None) -> str:
    """The base decider prompt plus the champion's (or a given policy's) lessons."""
    try:
        pol = policy if policy is not None else get_champion_policy(storage)
    except Exception:  # noqa: BLE001
        return base_prompt
    prompt = pol.get("system_prompt") or base_prompt
    lessons = pol.get("lessons") or []
    if lessons:
        prompt = prompt + "\n\nLearned lessons from your coach (apply them):\n" + \
            "\n".join(f"- {str(les)}" for les in lessons[:12])
    return prompt
