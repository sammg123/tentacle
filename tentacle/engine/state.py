"""Persistent engine state, built on the storage key/value store.

The engine OWNS the canonical capital model so there is one source of truth. The
risk module consumes the AccountState the engine constructs here; it never has to
guess kv key names. TENTACLE's capital is its OWN internal ledger (the current
rung), independent of whatever the brokerage balance happens to be.

kv keys (all namespaced "eng."):
  eng.rung               int   current ladder rung (default 0 = Live Shadow/Paper)
  eng.stage_start_equity float capital at the start of THIS stage (= rung capital)
  eng.cash               float settled cash available in the rung
  eng.alive              bool  False once the ruin floor kills the bot
  eng.armed              bool  operator must ARM before any real/paper order fires
  eng.halted_today       bool  daily-loss circuit breaker
  eng.halt_date          str   ISO date the halt/realized counters belong to
  eng.realized_today     float realized P&L booked today (for the daily halt)
  eng.last_cycle         str   ISO of the last completed cycle
  eng.cycles             int   total cycles run
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from ..config import Config, get_config, rung_by_level
from ..marketdata import MarketData, get_marketdata
from ..models import AccountState
from ..storage import Storage, get_storage
from ..util import get_logger, now_iso, now_utc

log = get_logger("tentacle.engine.state")

# These four are SHARED with the risk + ladder modules, so they are NOT namespaced
# (the contracts told those modules to read kv "rung"/"stage_start_equity"/"alive"/"armed").
K_RUNG = "rung"
K_STAGE_START = "stage_start_equity"
K_ALIVE = "alive"
K_ARMED = "armed"
# Engine-private bookkeeping stays namespaced.
K_CASH = "eng.cash"
K_HALTED = "eng.halted_today"
K_HALT_DATE = "eng.halt_date"
K_REALIZED = "eng.realized_today"
K_LAST_CYCLE = "eng.last_cycle"
K_CYCLES = "eng.cycles"
K_BACKTEST_PASSED = "eng.backtest_passed"


class EngineState:
    def __init__(self, storage: Optional[Storage] = None, cfg: Optional[Config] = None):
        self.storage = storage or get_storage()
        self.cfg = cfg or get_config()

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self, start_rung: int = 0) -> None:
        """Initialize defaults the first time, idempotently."""
        if self.storage.kv_get(K_RUNG) is None:
            self.set_rung(start_rung, reset_stage=True)
        if self.storage.kv_get(K_ALIVE) is None:
            self.storage.kv_set(K_ALIVE, True)
        if self.storage.kv_get(K_ARMED) is None:
            self.storage.kv_set(K_ARMED, self.cfg.engine.armed)
        if self.storage.kv_get(K_CASH) is None:
            self.storage.kv_set(K_CASH, float(rung_by_level(self.rung).capital))
        self._roll_day()

    # ------------------------------------------------------------------ rung
    @property
    def rung(self) -> int:
        return int(self.storage.kv_get(K_RUNG, 0))

    def set_rung(self, level: int, reset_stage: bool = True) -> None:
        cap = float(rung_by_level(level).capital)
        self.storage.kv_set(K_RUNG, int(level))
        if reset_stage:
            self.storage.kv_set(K_STAGE_START, cap)
            self.storage.kv_set(K_CASH, cap)
        log.info("rung set to %d (%s, $%.0f)", level, rung_by_level(level).name, cap)

    @property
    def stage_start_equity(self) -> float:
        return float(self.storage.kv_get(K_STAGE_START, rung_by_level(self.rung).capital))

    @property
    def rung_capital(self) -> float:
        return float(rung_by_level(self.rung).capital)

    # ------------------------------------------------------------------ cash
    @property
    def cash(self) -> float:
        v = self.storage.kv_get(K_CASH)
        return float(v) if v is not None else self.rung_capital

    def set_cash(self, amount: float) -> None:
        self.storage.kv_set(K_CASH, float(amount))

    def spend_cash(self, amount: float) -> None:
        self.set_cash(self.cash - float(amount))

    # ------------------------------------------------------------------ life / arm
    @property
    def alive(self) -> bool:
        return bool(self.storage.kv_get(K_ALIVE, True))

    def set_alive(self, val: bool) -> None:
        self.storage.kv_set(K_ALIVE, bool(val))

    @property
    def armed(self) -> bool:
        # persistent ARM flag is the source of truth; defaults to cfg at first run.
        v = self.storage.kv_get(K_ARMED)
        return bool(self.cfg.engine.armed) if v is None else bool(v)

    def set_armed(self, val: bool) -> None:
        self.storage.kv_set(K_ARMED, bool(val))

    # ------------------------------------------------------------------ daily halt
    def _roll_day(self) -> None:
        today = now_utc().date().isoformat()
        if self.storage.kv_get(K_HALT_DATE) != today:
            self.storage.kv_set(K_HALT_DATE, today)
            self.storage.kv_set(K_HALTED, False)
            self.storage.kv_set(K_REALIZED, 0.0)

    @property
    def halted_today(self) -> bool:
        self._roll_day()
        return bool(self.storage.kv_get(K_HALTED, False))

    def set_halted(self, val: bool) -> None:
        self.storage.kv_set(K_HALTED, bool(val))

    @property
    def realized_today(self) -> float:
        self._roll_day()
        return float(self.storage.kv_get(K_REALIZED, 0.0))

    def book_realized(self, pnl: float) -> None:
        self._roll_day()
        self.storage.kv_set(K_REALIZED, self.realized_today + float(pnl))

    # ------------------------------------------------------------------ counters
    @property
    def cycles(self) -> int:
        return int(self.storage.kv_get(K_CYCLES, 0))

    def tick_cycle(self) -> None:
        self.storage.kv_set(K_CYCLES, self.cycles + 1)
        self.storage.kv_set(K_LAST_CYCLE, now_iso())

    @property
    def backtest_passed(self) -> bool:
        return bool(self.storage.kv_get(K_BACKTEST_PASSED, False))

    def set_backtest_passed(self, val: bool) -> None:
        self.storage.kv_set(K_BACKTEST_PASSED, bool(val))

    # ------------------------------------------------------------------ account snapshot
    def account_state(self, md: Optional[MarketData] = None) -> AccountState:
        """Authoritative AccountState from the internal ledger + open trades."""
        md = md or get_marketdata()
        open_trades = self.storage.open_trades()
        unrealized = 0.0
        market_value = 0.0
        for t in open_trades:
            px = md.last_price(t["ticker"])
            if px is None:
                px = t["entry_price"]
            market_value += px * t["qty"]
            unrealized += (px - t["entry_price"]) * t["qty"]
        unsettled = self.storage.unsettled_total()
        settled = self.cash
        equity = settled + unsettled + market_value
        return AccountState(
            rung=self.rung,
            rung_capital=self.rung_capital,
            equity=equity,
            cash=settled + unsettled,
            settled_cash=settled,
            unsettled_cash=unsettled,
            open_positions=len(open_trades),
            realized_pnl_today=self.realized_today,
            armed=self.armed,
            alive=self.alive,
            halted_today=self.halted_today,
        )
