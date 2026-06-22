"""The Source ABC: the contract every tentacle implements.

A Source is one data feed. ``poll()`` returns brand-new RawSignals since the last
poll (the base class handles dedup + cursor persistence so subclasses just yield
everything they currently see). A source is *enabled* only if every env var it
requires is present, so key-gated tentacles stay dormant until lit up.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Iterable, Optional

from ..config import TIER_BASE_WEIGHT, get_config
from ..models import RawSignal
from ..storage import Storage, get_storage
from ..util import get_logger


class Source(ABC):
    # --- subclasses MUST set these ---
    id: str = "base"
    family: str = "news"          # a SourceFamily value
    tier: int = 2                 # a Tier value
    # env vars that must ALL be present for this source to be enabled. Empty =>
    # always-on free source (no key needed).
    requires_keys: tuple[str, ...] = ()
    # cold-start reliability override; if None, derived from the tier table.
    base_reliability: Optional[float] = None
    # polite minimum seconds between live polls (the engine may poll less often).
    min_interval_seconds: int = 60

    def __init__(self, storage: Optional[Storage] = None):
        self.storage = storage or get_storage()
        self.cfg = get_config()
        self.log = get_logger(f"tentacle.src.{self.id}")

    # ------------------------------------------------------------------ enabled
    @classmethod
    def is_enabled(cls) -> bool:
        return all(os.environ.get(k) for k in cls.requires_keys)

    def default_weight(self) -> float:
        if self.base_reliability is not None:
            return self.base_reliability
        return TIER_BASE_WEIGHT.get(self.tier, 0.5)

    # ------------------------------------------------------------------ fetch
    @abstractmethod
    def fetch(self) -> Iterable[RawSignal]:
        """Return EVERYTHING currently visible (dedup is handled upstream).

        Subclasses set ``source_id``, ``family``, ``tier`` on each RawSignal (or
        leave them and let ``poll`` stamp them). Network/parse errors should be
        swallowed and yield nothing rather than raise.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ poll
    def poll(self) -> list[RawSignal]:
        """Fetch, stamp defaults, dedup against storage, persist, return NEW only."""
        out: list[RawSignal] = []
        weight = self.storage.get_source_weight(self.id) * self.default_weight()
        try:
            items = list(self.fetch())
        except Exception as exc:  # noqa: BLE001 (a flaky tentacle must never crash the loop)
            self.log.debug("fetch failed: %s", exc)
            return out
        for sig in items:
            if sig is None:
                continue
            # stamp source identity + tuned reliability if the subclass didn't
            sig.source_id = sig.source_id or self.id
            if not sig.family:
                sig.family = self.family
            if not sig.tier:
                sig.tier = self.tier
            sig.base_reliability = weight
            if not sig.dedup_key:
                sig.__post_init__()
            if self.storage.save_signal(sig):   # True only if NEW
                out.append(sig)
        if out:
            self.log.info("%d new signal(s)", len(out))
        return out

    def __repr__(self) -> str:
        return f"<Source {self.id} tier={self.tier} family={self.family}>"
