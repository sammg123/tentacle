"""RobinhoodBroker: a documented stub for a future Robinhood execution path.

Robinhood has no official trading API. The community library robin_stocks
(https://github.com/jmfernandes/robin_stocks) wraps the private endpoints and
handles the device-token / MFA login flow. Wiring that up safely (credential
storage, 2FA, order placement) is a real project, so for now every order and
account method raises NotImplementedError with a clear pointer to robin_stocks.

The two read-only data methods (is_market_open, last_price) delegate to
marketdata, so a RobinhoodBroker can still serve as a price and clock venue (for
example during paper trading) without any Robinhood credentials.
"""
from __future__ import annotations

from typing import Optional

from ..config import Config, get_config
from ..storage import Storage, get_storage
from ..util import get_logger
from .base import Broker, BrokerOrder

log = get_logger("tentacle.broker.robinhood")

_POINTER = (
    "RobinhoodBroker is a stub. There is no official Robinhood trading API. "
    "Implement this via the robin_stocks library "
    "(https://github.com/jmfernandes/robin_stocks): handle the device-token / MFA "
    "login, then map robin_stocks.orders.order_buy_market and "
    "robin_stocks.orders.order_sell_market onto submit_buy / close_position. "
    "Until then, use BROKER=alpaca or BROKER=simulator."
)


class RobinhoodBroker(Broker):
    name = "robinhood"

    def __init__(self, cfg: Optional[Config] = None, storage: Optional[Storage] = None,
                 md=None):
        self.cfg = cfg or get_config()
        self.storage = storage or get_storage()
        if md is None:
            from ..marketdata import get_marketdata
            md = get_marketdata()
        self.md = md

    # ------------------------------------------------------------------ clock
    def is_market_open(self) -> bool:
        # Read-only: delegate to marketdata. Fail safe to False if unknown.
        try:
            return bool(self.md.is_market_open())
        except Exception:
            return False

    # ------------------------------------------------------------------ price
    def last_price(self, ticker: str) -> Optional[float]:
        # Read-only: delegate to marketdata.
        try:
            px = self.md.last_price(ticker)
            return float(px) if px is not None else None
        except Exception:
            return None

    # ------------------------------------------------------------------ account
    def get_account(self) -> dict:
        raise NotImplementedError(_POINTER)

    # ------------------------------------------------------------------ positions
    def list_positions(self) -> list[dict]:
        raise NotImplementedError(_POINTER)

    def get_position(self, ticker: str) -> Optional[dict]:
        raise NotImplementedError(_POINTER)

    # ------------------------------------------------------------------ orders
    def submit_buy(self, ticker: str, qty: float, *,
                   limit: Optional[float] = None) -> BrokerOrder:
        raise NotImplementedError(_POINTER)

    def close_position(self, ticker: str) -> BrokerOrder:
        raise NotImplementedError(_POINTER)
