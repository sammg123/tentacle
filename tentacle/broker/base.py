"""The Broker ABC: the execution and price venue contract.

TENTACLE is LONG ONLY. There is exactly one way to open a position (``submit_buy``)
and exactly one way to close it (``close_position``). No options, no crypto, no
shorting, no margin. The broker is purely an execution and price venue: it never
holds the bot's notion of capital (that lives in the rung ledger in storage).

Every concrete broker (simulator, alpaca, robinhood) implements this surface.
All network or parse errors are swallowed inside the implementations so a flaky
venue never crashes the loop: prices degrade to None (caller treats None as
"unknown" and fails safe), and order failures return a BrokerOrder with a
non-"filled" status rather than raising.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BrokerOrder:
    """The result of submitting an order to a broker.

    ``status`` is the venue-normalized order state. The simulator and a live fill
    use "filled". A rejected or errored order uses something else (e.g. "rejected",
    "error", "blocked") and the engine treats anything other than a real fill as a
    no-op so an unknown outcome never books a phantom position.
    """

    order_id: str
    ticker: str
    qty: float
    side: str                       # "buy" or "sell" (long-only: sell == close)
    filled_price: float
    status: str
    raw: dict = field(default_factory=dict)

    @property
    def filled(self) -> bool:
        """True only for a genuine fill we can trust enough to book."""
        return str(self.status).lower() in {"filled", "partially_filled"} and self.qty > 0


class Broker(ABC):
    """Long-only execution and price venue. Subclasses must guard everything."""

    name: str = "base"

    # ------------------------------------------------------------------ clock
    @abstractmethod
    def is_market_open(self) -> bool:
        """True only during regular US equity hours. Fail safe to False if unknown."""
        raise NotImplementedError

    # ------------------------------------------------------------------ price
    @abstractmethod
    def last_price(self, ticker: str) -> Optional[float]:
        """Most recent trade price, or None if unknown (caller fails safe)."""
        raise NotImplementedError

    # ------------------------------------------------------------------ account
    @abstractmethod
    def get_account(self) -> dict:
        """Return the venue account view.

        Keys: equity, cash, settled_cash, unsettled_cash, buying_power. This is the
        BROKER's view, not the bot's rung capital. The risk layer keeps its own
        ledger and treats this only as a sanity reference.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ positions
    @abstractmethod
    def list_positions(self) -> list[dict]:
        """All open long positions as dicts (ticker, qty, avg_entry_price, ...)."""
        raise NotImplementedError

    @abstractmethod
    def get_position(self, ticker: str) -> Optional[dict]:
        """The open position for one ticker, or None if flat."""
        raise NotImplementedError

    # ------------------------------------------------------------------ orders
    @abstractmethod
    def submit_buy(self, ticker: str, qty: float, *,
                   limit: Optional[float] = None) -> BrokerOrder:
        """Open (or add to) a long position. The ONLY way to buy.

        ``limit`` is an optional limit price; None means a market day order.
        """
        raise NotImplementedError

    @abstractmethod
    def close_position(self, ticker: str) -> BrokerOrder:
        """Flatten the entire long position in ``ticker``. The ONLY way to sell."""
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<Broker {self.name}>"
