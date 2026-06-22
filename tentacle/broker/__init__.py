"""The BROKER package: TENTACLE's execution and price venues.

TENTACLE is LONG ONLY. The single open path is ``submit_buy`` and the single
close path is ``close_position``. No shorting, options, crypto, or margin.

The broker is purely an execution and price venue. It does NOT define the bot's
capital: that lives in the rung ledger in storage, owned by the risk layer. Each
concrete broker (simulator, alpaca, robinhood) implements the same Broker ABC and
swallows network or parse errors so a venue hiccup never crashes the loop or books
a phantom fill. Unknown prices degrade to None (callers fail safe).

Pick one with ``get_broker(cfg)`` which honors cfg.broker and falls back to the
SimulatorBroker (paper) whenever a live venue is unavailable or unconfigured.
"""
from __future__ import annotations

from .alpaca import AlpacaBroker
from .base import Broker, BrokerOrder
from .factory import get_broker
from .robinhood import RobinhoodBroker
from .simulator import SimulatorBroker

__all__ = [
    "Broker",
    "BrokerOrder",
    "SimulatorBroker",
    "AlpacaBroker",
    "RobinhoodBroker",
    "get_broker",
]
