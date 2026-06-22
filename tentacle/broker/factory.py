"""Broker factory: pick the execution venue by config.

cfg.broker selects the venue:
  "alpaca"     -> AlpacaBroker if Alpaca keys are present, else SimulatorBroker
                  (with a warning, so a misconfigured live attempt degrades to
                  paper rather than firing into the void).
  "simulator"  -> SimulatorBroker (internal paper fills).
  "robinhood"  -> RobinhoodBroker (stub; orders raise NotImplementedError).
  anything else / unset -> SimulatorBroker (the safe default).

The factory never raises: on any unexpected error it falls back to the simulator.
"""
from __future__ import annotations

from typing import Optional

from ..config import Config, get_config
from ..storage import Storage, get_storage
from ..util import get_logger
from .base import Broker

log = get_logger("tentacle.broker.factory")


def get_broker(cfg: Optional[Config] = None, storage: Optional[Storage] = None,
               md=None) -> Broker:
    cfg = cfg or get_config()
    storage = storage or get_storage()
    if md is None:
        try:
            from ..marketdata import get_marketdata
            md = get_marketdata()
        except Exception:
            md = None

    choice = (getattr(cfg, "broker", "") or "").strip().lower()

    try:
        if choice == "alpaca":
            from .alpaca import AlpacaBroker
            if AlpacaBroker.has_keys(cfg):
                return AlpacaBroker(cfg=cfg, storage=storage, md=md)
            from .simulator import SimulatorBroker
            log.warning(
                "BROKER=alpaca but no Alpaca keys found; "
                "falling back to the SimulatorBroker (paper)."
            )
            return SimulatorBroker(cfg=cfg, storage=storage, md=md)

        if choice == "robinhood":
            from .robinhood import RobinhoodBroker
            return RobinhoodBroker(cfg=cfg, storage=storage, md=md)

        if choice == "simulator":
            from .simulator import SimulatorBroker
            return SimulatorBroker(cfg=cfg, storage=storage, md=md)

        # Default / unknown: the safe paper venue.
        if choice and choice not in {"alpaca", "simulator", "robinhood"}:
            log.warning("unknown BROKER=%r; defaulting to SimulatorBroker", choice)
        from .simulator import SimulatorBroker
        return SimulatorBroker(cfg=cfg, storage=storage, md=md)
    except Exception as exc:  # noqa: BLE001
        log.warning("broker init failed (%s); falling back to SimulatorBroker", exc)
        from .simulator import SimulatorBroker
        return SimulatorBroker(cfg=cfg, storage=storage, md=md)
