"""SimulatorBroker: internal paper fills, no external trading venue.

Prices and the market clock come from marketdata (so paper trading uses the same
data path as live). Fills model the painful reality that your order lands AFTER
the spike that triggered it: a buy fills ABOVE the reference price by a small slip
(default 0.15%), and a close fills BELOW the reference by the same slip. Cash and
positions persist across restarts in storage kv ("sim_cash", "sim_positions").

sim_cash is seeded lazily from the CURRENT RUNG's capital the first time the
broker touches the ledger, so paper trading uses the same capital the ladder has
earned. This is a convenience seed only: the risk layer keeps the authoritative
rung ledger and never trusts the broker for its capital.
"""
from __future__ import annotations

import math
import os
from typing import Optional

from ..config import Config, get_config, rung_by_level
from ..storage import Storage, get_storage
from ..util import get_logger, now_iso, stable_id
from .base import Broker, BrokerOrder

log = get_logger("tentacle.broker.simulator")

# Default one-sided slippage: your fill lands after the spike.
_DEFAULT_SLIP = float(os.environ.get("SIM_SLIPPAGE_PCT", "0.0015"))  # 0.15%


class SimulatorBroker(Broker):
    name = "simulator"

    def __init__(self, cfg: Optional[Config] = None, storage: Optional[Storage] = None,
                 md=None, slippage: Optional[float] = None):
        self.cfg = cfg or get_config()
        self.storage = storage or get_storage()
        if md is None:
            from ..marketdata import get_marketdata
            md = get_marketdata()
        self.md = md
        self.slippage = _DEFAULT_SLIP if slippage is None else float(slippage)

    # ------------------------------------------------------------------ seed
    def _current_rung_capital(self) -> float:
        """The capital of the rung we are currently on (lazy sim_cash seed)."""
        try:
            rung = int(self.storage.kv_get("rung", -1))
        except Exception:
            rung = -1
        try:
            cap = float(rung_by_level(rung).capital)
        except Exception:
            cap = 0.0
        # Rungs -1 and 0 have $0 capital (backtest / live shadow). Paper trading
        # still needs working cash to demonstrate fills, so fall back to the top
        # rung target (starting_capital) when the current rung is unfunded.
        if cap <= 0:
            try:
                cap = float(self.cfg.starting_capital)
            except Exception:
                cap = 0.0
        return cap

    def _cash(self) -> float:
        val = self.storage.kv_get("sim_cash", None)
        if val is None:
            seed = self._current_rung_capital()
            self.storage.kv_set("sim_cash", seed)
            return seed
        try:
            return float(val)
        except Exception:
            return 0.0

    def _set_cash(self, amount: float) -> None:
        self.storage.kv_set("sim_cash", float(amount))

    def _positions(self) -> dict:
        val = self.storage.kv_get("sim_positions", None)
        if not isinstance(val, dict):
            return {}
        return val

    def _set_positions(self, positions: dict) -> None:
        self.storage.kv_set("sim_positions", positions)

    # ------------------------------------------------------------------ clock
    def is_market_open(self) -> bool:
        try:
            return bool(self.md.is_market_open())
        except Exception:
            # FAIL SAFE: unknown clock state blocks trading.
            return False

    # ------------------------------------------------------------------ price
    def last_price(self, ticker: str) -> Optional[float]:
        try:
            px = self.md.last_price(ticker)
            return float(px) if px is not None else None
        except Exception:
            return None

    # ------------------------------------------------------------------ account
    def get_account(self) -> dict:
        cash = self._cash()
        positions = self._positions()
        market_value = 0.0
        for tkr, pos in positions.items():
            try:
                qty = float(pos.get("qty", 0))
            except Exception:
                qty = 0.0
            px = self.last_price(tkr)
            if px is None:
                # Unknown price: fall back to cost basis so equity is not phantom.
                try:
                    px = float(pos.get("avg_entry_price", 0))
                except Exception:
                    px = 0.0
            market_value += qty * px
        equity = cash + market_value
        # Paper account: treat all cash as settled. T+1 settlement is tracked by
        # the risk layer's own ledger, not the simulated venue.
        return {
            "equity": equity,
            "cash": cash,
            "settled_cash": cash,
            "unsettled_cash": 0.0,
            "buying_power": cash,
        }

    # ------------------------------------------------------------------ positions
    def list_positions(self) -> list[dict]:
        out: list[dict] = []
        for tkr, pos in self._positions().items():
            try:
                qty = float(pos.get("qty", 0))
            except Exception:
                qty = 0.0
            if qty <= 0:
                continue
            try:
                avg = float(pos.get("avg_entry_price", 0))
            except Exception:
                avg = 0.0
            px = self.last_price(tkr)
            cur = px if px is not None else avg
            out.append({
                "ticker": tkr,
                "qty": qty,
                "avg_entry_price": avg,
                "current_price": cur,
                "market_value": qty * cur,
                "cost_basis": qty * avg,
                "unrealized_pl": qty * (cur - avg),
            })
        return out

    def get_position(self, ticker: str) -> Optional[dict]:
        ticker = (ticker or "").upper().strip()
        for pos in self.list_positions():
            if pos["ticker"] == ticker:
                return pos
        return None

    # ------------------------------------------------------------------ orders
    def submit_buy(self, ticker: str, qty: float, *,
                   limit: Optional[float] = None) -> BrokerOrder:
        ticker = (ticker or "").upper().strip()
        try:
            qty = float(qty)
        except Exception:
            qty = 0.0
        if not ticker or qty <= 0:
            return self._rejected(ticker, qty, "buy", "bad ticker/qty")

        ref = self.last_price(ticker)
        if ref is None:
            # FAIL SAFE: cannot price the fill, so do not book a phantom buy.
            return self._rejected(ticker, qty, "buy", "no price")

        # Fill lands ABOVE the reference (after the spike). Respect a limit if given.
        fill = ref * (1.0 + self.slippage)
        if limit is not None:
            try:
                limit = float(limit)
                if fill > limit:
                    return self._rejected(ticker, qty, "buy",
                                          f"limit {limit} below sim fill {fill:.4f}")
            except Exception:
                pass

        cost = fill * qty
        cash = self._cash()
        if cost > cash + 1e-9:
            return self._rejected(ticker, qty, "buy",
                                  f"insufficient sim cash ({cost:.2f} > {cash:.2f})")

        positions = self._positions()
        existing = positions.get(ticker, {})
        try:
            old_qty = float(existing.get("qty", 0))
            old_avg = float(existing.get("avg_entry_price", 0))
        except Exception:
            old_qty, old_avg = 0.0, 0.0
        new_qty = old_qty + qty
        new_avg = ((old_qty * old_avg) + (qty * fill)) / new_qty if new_qty > 0 else fill
        positions[ticker] = {
            "qty": new_qty,
            "avg_entry_price": new_avg,
            "opened_at": existing.get("opened_at") or now_iso(),
        }
        self._set_positions(positions)
        self._set_cash(cash - cost)

        order_id = stable_id("sim_buy", ticker, now_iso(), qty)
        log.info("SIM BUY %s x%.4f @ %.4f (ref %.4f, slip %.4f)",
                 ticker, qty, fill, ref, self.slippage)
        return BrokerOrder(
            order_id=order_id, ticker=ticker, qty=qty, side="buy",
            filled_price=fill, status="filled",
            raw={"reference_price": ref, "slippage": self.slippage, "cost": cost},
        )

    def close_position(self, ticker: str) -> BrokerOrder:
        ticker = (ticker or "").upper().strip()
        positions = self._positions()
        pos = positions.get(ticker)
        if not pos:
            return self._rejected(ticker, 0.0, "sell", "no open position")
        try:
            qty = float(pos.get("qty", 0))
        except Exception:
            qty = 0.0
        if qty <= 0:
            positions.pop(ticker, None)
            self._set_positions(positions)
            return self._rejected(ticker, 0.0, "sell", "zero qty")

        ref = self.last_price(ticker)
        if ref is None:
            # FAIL SAFE: cannot price the exit. Leave the position open rather than
            # book a phantom close. The caller can retry next cycle.
            return self._rejected(ticker, qty, "sell", "no price")

        # Close fills BELOW the reference (the same after-the-move slip, against us).
        fill = ref * (1.0 - self.slippage)
        proceeds = fill * qty
        positions.pop(ticker, None)
        self._set_positions(positions)
        self._set_cash(self._cash() + proceeds)

        order_id = stable_id("sim_sell", ticker, now_iso(), qty)
        log.info("SIM CLOSE %s x%.4f @ %.4f (ref %.4f)", ticker, qty, fill, ref)
        return BrokerOrder(
            order_id=order_id, ticker=ticker, qty=qty, side="sell",
            filled_price=fill, status="filled",
            raw={"reference_price": ref, "slippage": self.slippage, "proceeds": proceeds},
        )

    # ------------------------------------------------------------------ helpers
    def _rejected(self, ticker: str, qty: float, side: str, reason: str) -> BrokerOrder:
        log.debug("SIM %s rejected for %s: %s", side, ticker, reason)
        return BrokerOrder(
            order_id="", ticker=ticker, qty=qty, side=side,
            filled_price=0.0, status="rejected", raw={"reason": reason},
        )
