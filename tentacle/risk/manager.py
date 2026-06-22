"""RiskManager: derives the account state from TENTACLE's OWN ledger and enforces
every hard gate before a trade may fire.

KEY POINT: the broker balance is NOT the bot's capital. TENTACLE keeps an internal
capital ledger (the rung). ``account_state`` builds an AccountState from storage:
  - rung and rung_capital from the kv store ("rung", "stage_start_equity"),
  - equity = stage_start + realized P&L of trades closed this stage + unrealized P&L
    of open trades (priced via marketdata),
  - cash / settled / unsettled from storage,
  - armed / alive / halted flags from config + kv.

``market_gates`` checks the tradeability of one candidate (market open, regime,
earliness, liquidity, no-chase vs VWAP). ``evaluate`` is the LAW: ALL hard gates must
pass for a fire, and the model can never override them. Everything FAILS SAFE: an
unknown price, regime, or market-open state BLOCKS the fire rather than allowing it.
"""
from __future__ import annotations

from typing import Optional

from ..models import AccountState, Candidate
from ..util import get_logger, now_utc, parse_dt
from . import caps
from .cash import available_settled_cash

log = get_logger("tentacle.risk.manager")


class RiskManager:
    def __init__(self, cfg=None, storage=None, md=None, broker=None):
        if cfg is None:
            from ..config import get_config
            cfg = get_config()
        if storage is None:
            from ..storage import get_storage
            storage = get_storage()
        if md is None:
            from ..marketdata import get_marketdata
            md = get_marketdata()
        self.cfg = cfg
        self.storage = storage
        self.md = md
        self.broker = broker  # execution venue only; not the capital source

    # ------------------------------------------------------------------ state
    def account_state(self) -> AccountState:
        """Build the AccountState from TENTACLE's internal ledger (NOT the broker).

        Never raises; degrades to a conservative, non-armed, capital-zero state on
        any error so nothing fires off a broken read.
        """
        try:
            kv = self.storage.kv_get

            rung = int(kv("rung", -1))

            # rung_capital: prefer the configured capital for the current rung.
            rung_capital = self._rung_capital_for(rung)

            # stage_start_equity: the equity baseline this stage began at. Default to
            # the rung capital if the kv has not been seeded yet.
            stage_start = kv("stage_start_equity", None)
            try:
                stage_start = float(stage_start) if stage_start is not None else float(rung_capital)
            except (TypeError, ValueError):
                stage_start = float(rung_capital)

            # Realized P&L of trades CLOSED this stage. We attribute closed trades by
            # their stored rung == current rung (each stage runs at one rung).
            realized_stage = 0.0
            realized_today = 0.0
            today = now_utc().date()
            try:
                for t in self.storage.closed_trades():
                    if int(t.get("rung", rung)) != rung:
                        continue
                    pnl = t.get("pnl")
                    if pnl is None:
                        continue
                    pnl = float(pnl)
                    realized_stage += pnl
                    exit_time = t.get("exit_time")
                    dt = parse_dt(exit_time)
                    if dt is not None and dt.date() == today:
                        realized_today += pnl
            except Exception:
                pass

            # Unrealized P&L of OPEN trades, priced via marketdata. An unknown price
            # contributes 0 (we do not invent a mark) rather than block here; the
            # FAIL-SAFE blocking happens in the fire gates, not in the snapshot.
            unrealized = 0.0
            open_positions = 0
            try:
                for t in self.storage.open_trades():
                    open_positions += 1
                    try:
                        qty = float(t.get("qty") or 0.0)
                        entry = float(t.get("entry_price") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    if qty <= 0 or entry <= 0:
                        continue
                    ticker = t.get("ticker")
                    last = None
                    try:
                        p = self.md.last_price(ticker) if ticker else None
                        last = float(p) if p is not None else None
                    except Exception:
                        last = None
                    if last is None or last != last:
                        continue
                    unrealized += (last - entry) * qty
            except Exception:
                pass

            equity = float(stage_start) + realized_stage + unrealized

            # Cash / settled / unsettled from storage.
            cash, settled, unsettled = self._cash_from_storage(kv, rung_capital)

            # Flags.
            armed = self._is_armed(kv)
            alive = bool(kv("alive", True))
            halted_today = self._halted_today(kv, today)

            return AccountState(
                rung=rung,
                rung_capital=float(rung_capital),
                equity=float(equity),
                cash=float(cash),
                settled_cash=float(settled),
                unsettled_cash=float(unsettled),
                open_positions=int(open_positions),
                realized_pnl_today=float(realized_today),
                armed=bool(armed),
                alive=bool(alive),
                halted_today=bool(halted_today),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("account_state failed, returning safe state: %s", exc)
            # FAIL SAFE: not armed, not alive enough to fire blind.
            return AccountState(
                rung=-1, rung_capital=0.0, equity=0.0, cash=0.0,
                settled_cash=0.0, unsettled_cash=0.0, open_positions=0,
                realized_pnl_today=0.0, armed=False, alive=True, halted_today=True,
            )

    # ------------------------------------------------------------------ helpers
    def _rung_capital_for(self, rung: int) -> float:
        try:
            from ..config import rung_by_level
            return float(rung_by_level(rung).capital)
        except Exception:
            return 0.0

    def _cash_from_storage(self, kv, rung_capital: float):
        """Derive (cash, settled, unsettled) from the kv store and settlements.

        TENTACLE tracks its own cash. "cash" is seeded lazily from rung capital when
        not yet set. Unsettled is the open settlements total; settled = cash minus
        unsettled, floored at zero.
        """
        try:
            cash = kv("cash", None)
            cash = float(cash) if cash is not None else float(rung_capital)
        except (TypeError, ValueError):
            cash = float(rung_capital)
        try:
            unsettled = float(self.storage.unsettled_total())
        except Exception:
            unsettled = 0.0
        if unsettled < 0:
            unsettled = 0.0
        settled = max(0.0, cash - unsettled)
        return (cash, settled, unsettled)

    def _is_armed(self, kv) -> bool:
        # The operator must explicitly ARM. Either the config flag or the kv override
        # can arm; default to disarmed (FAIL SAFE).
        try:
            cfg_armed = bool(self.cfg.engine.armed)
        except Exception:
            cfg_armed = False
        kv_armed = kv("armed", None)
        if kv_armed is None:
            return cfg_armed
        return bool(kv_armed)

    def _halted_today(self, kv, today) -> bool:
        """True if the daily-loss circuit breaker is set for today's date."""
        val = kv("halted_today", None)
        if val is None:
            return False
        # Stored as the ISO date the halt applies to, or a truthy flag.
        try:
            if isinstance(val, str):
                dt = parse_dt(val)
                if dt is not None:
                    return dt.date() == today
                return val.strip().lower() in {"1", "true", "yes", "on", "y"}
            return bool(val)
        except Exception:
            return bool(val)

    # ------------------------------------------------------------------ market gates
    def market_gates(self, cand: Candidate) -> tuple[bool, list[str]]:
        """Tradeability gates for one candidate. FAIL SAFE on unknown price.

        Returns (ok, reasons). reasons explains failures (and notes) for shadow logs.
        """
        reasons: list[str] = []
        ok = True
        try:
            gate = self.cfg.gate
            ticker = getattr(cand, "ticker", None)
            if not ticker:
                return (False, ["no ticker"])

            # Market open gate.
            if getattr(gate, "require_market_open", True):
                try:
                    is_open = self.md.is_market_open()
                except Exception:
                    is_open = None
                if is_open is None:
                    reasons.append("market-open unknown")
                    ok = False
                elif not is_open:
                    reasons.append("market closed")
                    ok = False
                else:
                    reasons.append("market open")

            # Regime gate.
            if getattr(gate, "require_uptrend_regime", True):
                from .regime import regime_is_up
                reg_ok, reg_note = regime_is_up(self.cfg, self.md)
                reasons.append(f"regime: {reg_note}")
                if not reg_ok:
                    ok = False

            # Price: required for liquidity / no-chase. Unknown -> FAIL SAFE.
            price = None
            try:
                p = self.md.last_price(ticker)
                price = float(p) if p is not None else None
            except Exception:
                price = None
            if price is None or price != price or price <= 0:
                reasons.append("price unknown")
                return (False, reasons)

            # Liquidity floor: minimum price and minimum average dollar volume.
            try:
                min_price = float(gate.liquidity_min_price)
            except Exception:
                min_price = 1.50
            if price < min_price:
                reasons.append(f"price {price:.2f} below liquidity floor {min_price:.2f}")
                ok = False

            try:
                adv = self.md.avg_dollar_volume(ticker)
                adv = float(adv) if adv is not None else None
            except Exception:
                adv = None
            try:
                min_adv = float(gate.liquidity_min_dollar_vol)
            except Exception:
                min_adv = 2_000_000.0
            if adv is None or adv != adv:
                reasons.append("dollar-volume unknown")
                ok = False  # FAIL SAFE: cannot confirm liquidity.
            elif adv < min_adv:
                reasons.append(f"avg $vol {adv:,.0f} below floor {min_adv:,.0f}")
                ok = False

            # No-chase vs VWAP: do not pay more than a small band above VWAP. If VWAP
            # is unknown we note it but do not block (price is known and within the
            # other gates); this matches a "best-effort no-chase" rather than blind.
            try:
                vwap = self.md.intraday_vwap(ticker)
                vwap = float(vwap) if vwap is not None else None
            except Exception:
                vwap = None
            try:
                band = float(gate.no_chase_vwap_band)
            except Exception:
                band = 0.03
            if vwap is not None and vwap == vwap and vwap > 0:
                ceiling = vwap * (1.0 + band)
                if price > ceiling:
                    reasons.append(f"no-chase: price {price:.2f} > vwap*({1+band:.2f})={ceiling:.2f}")
                    ok = False
                else:
                    reasons.append("within no-chase vwap band")
            else:
                reasons.append("vwap unknown (no-chase not enforced)")

            # Earliness: do not chase a name that already ran up far past the event-day
            # reference. Proxy the reference with the event-date daily-bar open. If the
            # reference is unknown we do NOT block on earliness, only note it.
            self._earliness_check(cand, ticker, price, gate, reasons)
            # Earliness only blocks when explicitly breached (handled inside helper).
            if any(r.startswith("earliness breached") for r in reasons):
                ok = False

            return (ok, reasons)
        except Exception as exc:  # noqa: BLE001
            # Any unexpected failure: block (FAIL SAFE).
            reasons.append(f"market_gates error: {exc}")
            return (False, reasons)

    def _earliness_check(self, cand: Candidate, ticker: str, price: float,
                         gate, reasons: list[str]) -> None:
        """Note (and possibly flag) how far price has run vs the event-day open.

        Proxy: the daily bar OPEN on the candidate's first event date. If we cannot
        resolve a reference, we note it but do not block (per the contract).
        """
        try:
            ref_iso = getattr(cand, "first_event_time", None)
            ref_dt = parse_dt(ref_iso)
            if ref_dt is None:
                reasons.append("earliness reference unknown (not blocking)")
                return
            ref_date = ref_dt.date()
            # Pull a window of daily bars and find the bar for the event date.
            df = self.md.daily_bars(ticker, 30)
            if df is None or not len(df):
                reasons.append("earliness reference unknown (not blocking)")
                return
            ref_open = None
            try:
                for idx in df.index:
                    try:
                        bar_date = idx.date()
                    except Exception:
                        bar_date = parse_dt(str(idx))
                        bar_date = bar_date.date() if bar_date is not None else None
                    if bar_date == ref_date:
                        ref_open = float(df.loc[idx, "open"])
                        break
            except Exception:
                ref_open = None
            if ref_open is None or ref_open != ref_open or ref_open <= 0:
                reasons.append("earliness reference unknown (not blocking)")
                return
            runup = (price - ref_open) / ref_open
            try:
                max_runup = float(gate.earliness_max_runup)
            except Exception:
                max_runup = 0.04
            if runup > max_runup:
                reasons.append(
                    f"earliness breached: runup {runup*100:.1f}% > {max_runup*100:.1f}%")
            else:
                reasons.append(f"earliness ok: runup {runup*100:.1f}%")
        except Exception:
            reasons.append("earliness reference unknown (not blocking)")

    # ------------------------------------------------------------------ the LAW
    def evaluate(self, cand: Candidate, account: AccountState) -> tuple[bool, list[str]]:
        """The LAW. Return (allow_fire, reasons). The model cannot override this.

        ALL of these must hold to allow a fire:
          - account.alive (the bot has not died at the ruin floor)
          - account.armed (the operator explicitly armed)
          - not account.halted_today (daily-loss circuit breaker not tripped)
          - not daily_loss_breached
          - not ruin_breached (vs the stage start)
          - open_positions < cfg.risk.max_open_positions
          - not storage.has_open_position(ticker)
          - the confirmation gate passes (brain.confirmation.passes_confirmation_gate)
          - the confidence size band is > 0
          - market_gates pass
        Everything FAILS SAFE: any unknown input blocks the fire.
        """
        reasons: list[str] = []
        allow = True

        try:
            # 1. Alive.
            if not getattr(account, "alive", False):
                reasons.append("BLOCK: bot is dead (ruin floor)")
                allow = False

            # 2. Armed.
            if not getattr(account, "armed", False):
                reasons.append("BLOCK: not armed")
                allow = False

            # 3. Daily-loss halt already set.
            if getattr(account, "halted_today", True):
                reasons.append("BLOCK: halted for today")
                allow = False

            # 4. Daily loss breach (recompute, do not trust a stale flag alone).
            try:
                if caps.daily_loss_breached(account, self.cfg):
                    reasons.append("BLOCK: daily loss limit breached")
                    allow = False
            except Exception:
                reasons.append("BLOCK: daily-loss check failed")
                allow = False

            # 5. Ruin breach vs stage start.
            try:
                stage_start = self.storage.kv_get("stage_start_equity", None)
                stage_start = (float(stage_start) if stage_start is not None
                               else float(account.rung_capital))
            except Exception:
                stage_start = float(getattr(account, "rung_capital", 0.0) or 0.0)
            try:
                if caps.ruin_breached(account.equity, stage_start, self.cfg):
                    reasons.append("BLOCK: ruin floor breached")
                    allow = False
            except Exception:
                reasons.append("BLOCK: ruin check failed")
                allow = False

            # 6. Open-position count cap.
            try:
                max_open = int(self.cfg.risk.max_open_positions)
            except Exception:
                max_open = 3
            if int(getattr(account, "open_positions", 0)) >= max_open:
                reasons.append(f"BLOCK: open positions at cap ({max_open})")
                allow = False

            # 7. No existing open position in this ticker.
            ticker = getattr(cand, "ticker", None)
            if not ticker:
                reasons.append("BLOCK: no ticker")
                allow = False
            else:
                try:
                    if self.storage.has_open_position(ticker):
                        reasons.append(f"BLOCK: already holding {ticker}")
                        allow = False
                except Exception:
                    reasons.append("BLOCK: position check failed")
                    allow = False

            # 8. Confirmation gate (brain). Imported lazily and guarded so the risk
            #    package imports even before the brain package is built. If it cannot
            #    be reached, FAIL SAFE and block.
            try:
                from ..brain.confirmation import passes_confirmation_gate
                conf_ok, conf_reasons = passes_confirmation_gate(cand, self.cfg)
                for r in conf_reasons:
                    reasons.append(f"confirm: {r}")
                if not conf_ok:
                    allow = False
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"BLOCK: confirmation gate unavailable ({exc})")
                allow = False

            # 9. Confidence size band > 0.
            try:
                from ..config import size_fraction_for
                frac = float(size_fraction_for(float(cand.confidence)))
            except Exception:
                frac = 0.0
            if frac <= 0:
                reasons.append("BLOCK: confidence below the minimum size band")
                allow = False
            else:
                reasons.append(f"size band ok: {frac:.2f} of rung")

            # 10. Market gates (open, regime, earliness, liquidity, no-chase).
            try:
                mg_ok, mg_reasons = self.market_gates(cand)
                for r in mg_reasons:
                    reasons.append(f"market: {r}")
                if not mg_ok:
                    allow = False
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"BLOCK: market gates failed ({exc})")
                allow = False

            return (bool(allow), reasons)
        except Exception as exc:  # noqa: BLE001
            # The LAW must never crash into an allow. Any surprise blocks.
            reasons.append(f"BLOCK: evaluate error ({exc})")
            return (False, reasons)
