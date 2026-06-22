"""The cycle loop. One ``run_cycle()`` is the full TENTACLE heartbeat:

  settle T+1 -> poll tentacles -> classify -> confirm/group -> score -> judge
  -> apply the risk LAW -> fire or shadow -> manage open positions -> resolve
  outcomes -> (periodically) learn + evaluate the ladder -> snapshot the ledger.

Doing nothing for an entire cycle (or weeks) is a valid, expected output.
The kill switch, the confidence gate, and the ladder caps are enforced HERE,
outside the model, which can never hope or average-down its way past them.
"""
from __future__ import annotations

import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Optional

from ..config import Config, catalyst_spec, get_config
from ..marketdata import MarketData, get_marketdata
from ..models import (
    Catalyst, ClassifiedSignal, DecisionType, RawSignal, Trade, TradeStatus,
)
from ..storage import Storage, get_storage
from ..util import age_seconds, get_logger, now_iso, now_utc, parse_dt

# brain
from ..brain.classifier import classify
from ..brain.confirmation import build_candidates, passes_confirmation_gate
from ..brain.confidence import score_candidate
from ..brain.decider import decide_candidates
# risk
from ..risk.manager import RiskManager
from ..risk.sizing import position_size
from ..risk.caps import (
    daily_loss_breached, per_trade_stop_price, per_trade_target_price, ruin_breached,
)
from ..risk.cash import settle_due
# learning + ladder
from ..learning.outcomes import log_outcome, resolve_due_outcomes
from ..learning.tuner import retune
from ..ladder.promotion import evaluate_ladder
from ..ladder.mastery import compute_mastery
# broker + sources
from ..broker.factory import get_broker
from ..sources.registry import build_sources

from .state import EngineState

log = get_logger("tentacle.engine.loop")

LEARN_EVERY = int(os.environ.get("TENTACLE_LEARN_EVERY", "10"))   # cycles
POLL_TIMEOUT = float(os.environ.get("TENTACLE_POLL_TIMEOUT", "45"))


class Engine:
    def __init__(self, cfg: Optional[Config] = None, storage: Optional[Storage] = None):
        self.cfg = cfg or get_config()
        self.storage = storage or get_storage()
        self.md: MarketData = get_marketdata()
        self.state = EngineState(self.storage, self.cfg)
        self.state.bootstrap(start_rung=0)
        self.broker = get_broker(self.cfg, self.storage, self.md)
        self.risk = RiskManager(self.cfg, self.storage, self.md, self.broker)
        self.sources = build_sources(self.storage)
        # rolling buffer of recent ClassifiedSignals for cross-cycle confirmation
        self.buffer: deque = deque(maxlen=5000)
        self._rehydrate_buffer()
        log.info("engine ready: rung %d, %d tentacle(s) live, broker=%s, armed=%s",
                 self.state.rung, len(self.sources), type(self.broker).__name__, self.state.armed)

    # ------------------------------------------------------------------ buffer
    def _window_start_iso(self) -> str:
        mins = self.cfg.gate.confirm_window_min
        return (now_utc() - timedelta(minutes=mins)).isoformat()

    @staticmethod
    def _row_to_raw(row: dict) -> RawSignal:
        import json
        return RawSignal(
            source_id=row["source_id"], family=row["family"], tier=row["tier"],
            title=row["title"], url=row["url"] or "", summary=row["summary"] or "",
            tickers=json.loads(row["tickers"] or "[]"),
            event_time=row["event_time"], fetched_at=row["fetched_at"],
            base_reliability=row["base_reliability"] or 0.5,
            signal_strength=row["signal_strength"] or 0.5,
            catalyst_hint=row["catalyst_hint"], dedup_key=row["dedup_key"],
            raw=json.loads(row["raw"] or "{}"),
        )

    def _rehydrate_buffer(self) -> None:
        try:
            for row in self.storage.recent_signals(self._window_start_iso()):
                raw = self._row_to_raw(row)
                for cs in classify(raw, self.cfg):
                    self.buffer.append((raw.fetched_at, cs))
        except Exception as exc:  # noqa: BLE001
            log.debug("buffer rehydrate skipped: %s", exc)

    def _trim_buffer(self) -> None:
        cutoff = self._window_start_iso()
        while self.buffer and self.buffer[0][0] < cutoff:
            self.buffer.popleft()

    # ------------------------------------------------------------------ polling
    def poll_sources(self) -> list[RawSignal]:
        new: list[RawSignal] = []
        if not self.sources:
            return new
        with ThreadPoolExecutor(max_workers=min(10, len(self.sources))) as ex:
            futs = {ex.submit(s.poll): s for s in self.sources}
            try:
                for fut in as_completed(futs, timeout=POLL_TIMEOUT):
                    try:
                        new.extend(fut.result() or [])
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # overall timeout: take whatever finished
                for fut, s in futs.items():
                    if fut.done():
                        try:
                            new.extend(fut.result() or [])
                        except Exception:
                            pass
        return new

    # ------------------------------------------------------------------ cycle
    def run_cycle(self) -> dict:
        self.state._roll_day()
        summary = {"new_signals": 0, "candidates": 0, "fired": 0, "shadow": 0,
                   "veto": 0, "closed": 0, "resolved": 0, "notes": []}

        # 0. KILL SWITCH (real-time death guard, enforced outside the model)
        account = self.state.account_state(self.md)
        if not account.alive:
            summary["notes"].append("bot is DEAD (ruin floor); trading halted")
            self.state.tick_cycle()
            return summary
        if account.rung_capital > 0 and ruin_breached(
                account.equity, self.state.stage_start_equity, self.cfg):
            self._die()
            summary["notes"].append("RUIN FLOOR breached -> DEATH; flattened, demoted")
            self.state.tick_cycle()
            return summary

        # 0b. settle T+1 proceeds
        settled = settle_due(self.storage, self.cfg)
        if settled:
            self.state.set_cash(self.state.cash + settled)
            summary["notes"].append(f"settled ${settled:.2f}")

        # 1. poll tentacles
        new_raw = self.poll_sources()
        summary["new_signals"] = len(new_raw)

        # 2. classify into the rolling buffer
        for raw in new_raw:
            for cs in classify(raw, self.cfg):
                self.buffer.append((now_iso(), cs))
        self._trim_buffer()
        classified: list[ClassifiedSignal] = [cs for _, cs in self.buffer]

        # 3. group into candidates
        cands = build_candidates(classified, self.cfg)
        summary["candidates"] = len(cands)

        # 4. score
        for c in cands:
            try:
                score_candidate(c, self.cfg, self.storage, self.md)
            except Exception as exc:  # noqa: BLE001
                log.debug("score failed %s: %s", c.ticker, exc)

        # 5. DECIDE: Claude (subscription CLI, then API, then heuristic) decides
        # BUY/PASS + conviction on the top-N confirmation-passing finalists, within
        # the stated rules envelope. Conviction replaces the algo score for sizing.
        account = self.state.account_state(self.md)
        try:
            cands = decide_candidates(cands, self.cfg, self.md, self.storage, account)
        except Exception as exc:  # noqa: BLE001
            log.debug("decider failed: %s", exc)
        cands.sort(key=lambda c: c.confidence, reverse=True)

        # 6. execute / shadow
        for c in cands:
            self._decide(c, account, summary)
            account = self.state.account_state(self.md)  # refresh caps after a fire

        # 7. manage open positions (stops / targets / horizon)
        summary["closed"] = self.manage_positions()

        # 8. resolve outcomes whose horizon has elapsed (fired AND shadow)
        try:
            summary["resolved"] = resolve_due_outcomes(self.storage, self.md, self.cfg)
        except Exception as exc:  # noqa: BLE001
            log.debug("resolve failed: %s", exc)

        # 8b. the mother's financial guardian (Eye 2): cheap, aggregate, noise-gated.
        # Alerts only on threshold crossings; does not bypass the hard risk law.
        if self.cfg.mother.guardian_in_loop:
            try:
                from ..mother.guardian import check_and_alert
                g = check_and_alert(self.storage, self.cfg, self.md, notify=self._notify)
                for code in (g or {}).get("new_alerts", []):
                    summary["notes"].append(f"guardian alert: {code}")
            except Exception as exc:  # noqa: BLE001
                log.debug("guardian failed: %s", exc)

        # 9. periodic learning + ladder (only when flat, to keep capital math clean)
        if self.state.cycles % LEARN_EVERY == 0:
            try:
                retune(self.storage, self.cfg)
            except Exception as exc:  # noqa: BLE001
                log.debug("retune failed: %s", exc)
            if not self.storage.open_trades():
                self._evaluate_ladder(summary)

        # 9b. the weekly Mother pass (blind review + propose + promote + graduation),
        # run IN-PROCESS so it shares the DB. A separate Railway cron service cannot:
        # a Railway volume mounts to only one service, so it would see an empty DB.
        self._maybe_run_mother(summary)

        # 10. snapshot ledger + tick
        acct = self.state.account_state(self.md)
        self.storage.append_ledger(acct.rung, acct.equity, acct.cash, acct.settled_cash,
                                   acct.unsettled_cash, self.state.realized_today, "cycle")
        self.state.tick_cycle()

        # 11. optional Supabase mirror (decisions/trades -> Bloomberg terminal). No-op without
        # creds; fully guarded so it can never break the loop. See tentacle/mirror.py.
        try:
            from ..mirror import sync as _mirror_sync
            _mirror_sync(self.storage, self.cfg)
        except Exception as exc:  # noqa: BLE001
            log.debug("mirror sync failed: %s", exc)

        return summary

    # ------------------------------------------------------------------ decide one
    def _decide(self, c, account, summary) -> None:
        conf_ok, conf_reasons = passes_confirmation_gate(c, self.cfg)
        for r in conf_reasons:
            c.add_reason(r)
        if not conf_ok:
            c.decision = DecisionType.SHADOW.value
            self.storage.save_candidate(c)
            summary["shadow"] += 1
            return

        # a genuine multi-source, multi-family hypothesis
        price = self.md.last_price(c.ticker)
        allow, reasons = self.risk.evaluate(c, account)
        for r in reasons:
            c.add_reason(r)

        # HARD GATE: real money is blocked until the mother approves graduation.
        # Neither the kid nor an impatient operator can reach a live account first.
        live_block = self._real_money() and not self._graduated()
        if live_block:
            c.add_reason("REAL-MONEY BLOCKED: the mother has not approved graduation "
                         "to a live account (run `tentacle graduate`)")

        placed = False
        fill = price
        # Fire-eligible ONLY when the decision-maker said BUY (the bounded top-N
        # finalists) AND real-money is unblocked. Everything else shadow-logs.
        if allow and account.rung_capital > 0 and price and c.verdict == "buy" and not live_block:
            dollars, qty = position_size(c.confidence, account, self.cfg, self.md,
                                         c.ticker, price)
            if qty > 0 and dollars > 0:
                try:
                    order = self.broker.submit_buy(c.ticker, qty)
                    fill = order.filled_price or price
                    spec = catalyst_spec(c.catalyst)
                    trade = Trade(
                        ticker=c.ticker, qty=qty, entry_price=fill,
                        size_dollars=fill * qty, confidence=c.confidence,
                        catalyst=c.catalyst, rung=self.state.rung,
                        horizon_hours=spec.horizon_hours,
                        stop_price=per_trade_stop_price(fill, self.cfg),
                        target_price=per_trade_target_price(fill, self.cfg),
                        paper=(self.cfg.broker != "alpaca"
                               or self.cfg.integrations.alpaca_paper),
                        broker_order_id=order.order_id,
                    )
                    self.storage.save_trade(trade)
                    self.state.spend_cash(fill * qty)
                    c.decision = DecisionType.FIRE.value
                    placed = True
                    summary["fired"] += 1
                    log.info("FIRE %s x%.4f @ %.2f conf=%.0f (%s)",
                             c.ticker, qty, fill, c.confidence, c.catalyst)
                    self._notify(f"FIRE {c.ticker} x{qty:.4f} @ {fill:.2f} "
                                 f"conf={c.confidence:.0f} {c.catalyst}")
                except Exception as exc:  # noqa: BLE001
                    log.warning("order failed %s: %s", c.ticker, exc)

        if not placed:
            if account.rung_capital == 0 or c.verdict != "buy" or c.size_fraction == 0:
                # no capital, the decider said PASS (or it was not a finalist), or
                # conviction landed below the smallest size band
                c.decision = DecisionType.SHADOW.value
                summary["shadow"] += 1
            else:
                # decider said BUY with size and capital, but a hard risk gate blocked it
                c.decision = DecisionType.VETO.value
                summary["veto"] += 1

        # learn from every multi-source hypothesis, fired or not (road not taken)
        if price:
            try:
                log_outcome(c, fill if placed else price, placed, self.storage, self.cfg)
            except Exception as exc:  # noqa: BLE001
                log.debug("log_outcome failed: %s", exc)
        self.storage.save_candidate(c)

        # journal the kid's decision (only the ones he actually ruled on) so the
        # mother can review his reasoning later. Outcomes are NOT stored here.
        if c.verdict in ("buy", "pass"):
            self._journal_decision(c, placed, fill if placed else price)

    def _journal_decision(self, c, fired: bool, entry_price) -> None:
        try:
            cid = c.cid()  # canonical id shared with outcomes + the A/B log
            evidence = []
            for cs in c.signals[:6]:
                raw = getattr(cs, "raw", None)
                if raw is not None and raw.title:
                    evidence.append({"family": cs.family, "src": raw.source_id,
                                     "title": raw.title[:180]})
            llm = (c.pillar_scores or {}).get("llm", {})
            rules = {
                "min_sources": self.cfg.gate.min_sources,
                "min_families": self.cfg.gate.min_families,
                "earliness_max_runup": self.cfg.gate.earliness_max_runup,
                "per_trade_stop_pct": self.cfg.risk.per_trade_stop_pct,
                "size_bands": "95:1.0,85:0.7,70:0.45,55:0.2",
            }
            from ..policy import policy_version
            self.storage.save_decision(
                candidate_id=cid, ticker=c.ticker, catalyst=c.catalyst,
                verdict=c.verdict, conviction=float(c.confidence), thesis=c.judge_notes,
                evidence=evidence, families=sorted(c.families), rules=rules,
                backend=str(llm.get("backend", "heuristic")), fired=fired,
                entry_price=(float(entry_price) if entry_price else None),
                rung=self.state.rung, policy_version=policy_version(self.storage),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("journal write failed: %s", exc)

    # ------------------------------------------------------------------ live-money gate
    def _real_money(self) -> bool:
        """True only when pointed at a REAL (non-paper) Alpaca account."""
        return (self.cfg.broker == "alpaca"
                and not self.cfg.integrations.alpaca_paper)

    def _graduated(self) -> bool:
        try:
            from ..mother.graduation import is_graduated
            return is_graduated(self.storage)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ positions
    def manage_positions(self) -> int:
        closed = 0
        for t in self.storage.open_trades():
            px = self.md.last_price(t["ticker"])
            if px is None:
                continue
            reason = None
            if px <= (t["stop_price"] or 0):
                reason = "stop"
            elif t["target_price"] and px >= t["target_price"]:
                reason = "target"
            elif age_seconds(t["entry_time"]) / 3600.0 >= (t["horizon_hours"] or 1e9):
                reason = "horizon"
            if reason:
                self._close_trade(t, px, reason)
                closed += 1
        return closed

    def _close_trade(self, t: dict, px: float, reason: str) -> None:
        exit_px = px
        try:
            order = self.broker.close_position(t["ticker"])
            exit_px = order.filled_price or px
        except Exception as exc:  # noqa: BLE001
            log.warning("close failed %s: %s", t["ticker"], exc)
        pnl = (exit_px - t["entry_price"]) * t["qty"]
        trade = self._row_to_trade(t)
        trade.status = TradeStatus.CLOSED.value
        trade.exit_price = exit_px
        trade.exit_time = now_iso()
        trade.pnl = pnl
        trade.outcome = "win" if pnl > 0 else ("loss" if pnl < 0 else "flat")
        self.storage.save_trade(trade)
        # proceeds settle T+1 for cash accounts
        proceeds = exit_px * t["qty"]
        settles_on = (now_utc() + timedelta(days=self.cfg.risk.settle_days)).date().isoformat()
        if self.cfg.risk.cash_account:
            self.storage.add_settlement(proceeds, settles_on)
        else:
            self.state.set_cash(self.state.cash + proceeds)
        self.state.book_realized(pnl)
        log.info("CLOSE %s @ %.2f (%s) pnl=%.2f", t["ticker"], exit_px, reason, pnl)
        self._notify(f"CLOSE {t['ticker']} @ {exit_px:.2f} ({reason}) pnl={pnl:.2f}")
        # daily-loss circuit breaker
        if daily_loss_breached(self.state.account_state(self.md), self.cfg):
            self.state.set_halted(True)
            log.warning("DAILY LOSS HALT tripped")
            self._notify("DAILY LOSS HALT tripped; no new trades today")

    @staticmethod
    def _row_to_trade(row: dict) -> Trade:
        return Trade(
            ticker=row["ticker"], qty=row["qty"], entry_price=row["entry_price"],
            size_dollars=row["size_dollars"], confidence=row["confidence"],
            catalyst=row["catalyst"], rung=row["rung"], horizon_hours=row["horizon_hours"],
            stop_price=row["stop_price"], target_price=row["target_price"],
            entry_time=row["entry_time"], paper=bool(row["paper"]), status=row["status"],
            exit_price=row["exit_price"], exit_time=row["exit_time"], pnl=row["pnl"],
            outcome=row["outcome"], candidate_id=row["candidate_id"],
            broker_order_id=row["broker_order_id"], id=row["id"],
        )

    # ------------------------------------------------------------------ death / ladder
    def _die(self) -> None:
        """Ruin floor breached. Flatten everything, mark dead, demote one rung."""
        for t in self.storage.open_trades():
            px = self.md.last_price(t["ticker"]) or t["entry_price"]
            self._close_trade(t, px, "death-flatten")
        self.state.set_alive(False)
        new_rung = max(-1, self.state.rung - 1)
        self.state.set_rung(new_rung, reset_stage=True)
        log.error("DEATH: ruin floor breached. Demoted to rung %d; must re-qualify.", new_rung)
        self._notify(f"DEATH: ruin floor breached. Demoted to rung {new_rung}; re-qualify to return.")

    def _maybe_run_mother(self, summary) -> None:
        """Run the full Mother pass on a weekly cadence, in-process (shares the DB).
        The cheap financial guardian still runs every cycle separately; this is the
        heavy LLM review/propose/promote/graduation pass, gated so it stays weekly."""
        try:
            last = self.storage.kv_get("mother.last_full_run")
            cadence_h = float(self.cfg.mother.promotion_cooldown_hours)
            if last:
                dt = parse_dt(last)
                if dt is not None and (now_utc() - dt).total_seconds() / 3600.0 < cadence_h:
                    return
            # Only when flat: a promotion can change the kid's policy, so keep the
            # capital math clean by reviewing between positions.
            if self.storage.open_trades():
                return
            from ..mother import run_mother
            run_mother(self.storage, self.cfg, self.md, notify=self._notify,
                       propose=True, promote=True)
            self.storage.kv_set("mother.last_full_run", now_iso())
            summary["notes"].append("weekly mother pass ran")
        except Exception as exc:  # noqa: BLE001
            log.debug("mother pass failed: %s", exc)

    def _evaluate_ladder(self, summary) -> None:
        try:
            before = self.state.rung
            res = evaluate_ladder(self.storage, self.cfg, self.md)
            after = self.state.rung
            action = (res or {}).get("action", "hold")
            if after != before or action in ("promote", "demote", "death"):
                # promotion module moved the rung; sync engine-private cash + life
                self.state.set_cash(self.state.rung_capital)
                if action == "death":
                    self.state.set_alive(False)
                summary["notes"].append(
                    f"ladder: {action} -> rung {self.state.rung} "
                    f"(mastery {(res or {}).get('mastery', 0):.0f})")
                self._notify(f"LADDER {action} -> rung {self.state.rung}")
            mastery, comp = compute_mastery(self.storage, self.cfg)
            self.storage.record_mastery(self.state.rung, mastery, comp)
        except Exception as exc:  # noqa: BLE001
            log.debug("ladder eval failed: %s", exc)

    # ------------------------------------------------------------------ notify
    def _notify(self, msg: str) -> None:
        try:
            from ..notify import dispatch
            dispatch(msg, self.cfg)
        except Exception:  # noqa: BLE001
            pass
