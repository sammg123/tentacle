"""Eye 2 of the Mother: the financial guardian.

Pure computation over the journal + outcomes + closed trades, plus noise-gated
alerts. The guardian SEES outcomes, but only in AGGREGATE (drawdown, a loss
streak, rolling expectancy, calibration), never per individual trade. It alerts
ONLY on a THRESHOLD CROSSING (a code going inactive to active), persisting the
active code set in kv "mother.guardian_state" so the same condition is not
re-announced every cycle. Recovered codes are cleared so a later relapse alerts
again.

This is an EARLY BRAKE that sits ABOVE the hard ruin law (the -40% ruin floor,
the daily halt, the ladder caps, the settled-cash rule). It never replaces them
and it never touches sizing, stops, or any risk value. The single optional side
effect is: if cfg.mother.auto_defensive is on and a NEW drawdown alert fires, it
sets kv "armed" to False (a soft "stop placing new fires" brake the operator can
re-arm), and notes it. Everything is guarded: no function here ever raises, and
on missing data the guardian fails safe (it does not invent alerts, it notes the
gap and treats the unknown condition as "not alerting").
"""
from __future__ import annotations

from typing import Callable, Optional

from ..util import get_logger

log = get_logger("tentacle.mother.guardian")

# kv key that persists the set of currently-active alert codes between cycles.
K_GUARDIAN_STATE = "mother.guardian_state"
# kv key the engine reads to decide whether new fires may be placed. Shared,
# NOT namespaced (see engine/state.py K_ARMED). The guardian may only flip this
# to False as a soft brake; it never re-arms and never touches any risk value.
K_ARMED = "armed"

# Alert codes (kept as module constants so callers can compare without typos).
CODE_DRAWDOWN = "DRAWDOWN"
CODE_LOSS_STREAK = "LOSS_STREAK"
CODE_NEG_EXPECTANCY = "NEG_EXPECTANCY"
CODE_MISCALIBRATED = "MISCALIBRATED"


def _get_cfg(cfg):
    """Return a config object, importing lazily and guarding all failures."""
    if cfg is not None:
        return cfg
    try:
        from ..config import get_config
        return get_config()
    except Exception as exc:  # noqa: BLE001
        log.debug("get_config failed: %s", exc)
        return None


def _mother_cfg(cfg):
    """Return the MotherConfig (with safe attribute defaults if absent)."""
    return getattr(cfg, "mother", None)


def _attr(obj, name, default):
    """Read an attribute, returning default on anything missing or unusable."""
    try:
        val = getattr(obj, name, default)
        return default if val is None else val
    except Exception:  # noqa: BLE001
        return default


def _safe_float(val, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Aggregate metric helpers (each one guards its own data access)
# --------------------------------------------------------------------------- #
def _peak_equity(storage) -> Optional[float]:
    """Highest equity ever recorded in the equity_ledger, or None if empty."""
    try:
        curve = storage.equity_curve()
    except Exception as exc:  # noqa: BLE001
        log.debug("equity_curve failed: %s", exc)
        return None
    peak = None
    for row in curve or []:
        try:
            eq = row.get("equity") if isinstance(row, dict) else None
        except Exception:  # noqa: BLE001
            eq = None
        if eq is None:
            continue
        try:
            eqf = float(eq)
        except (TypeError, ValueError):
            continue
        if peak is None or eqf > peak:
            peak = eqf
    return peak


def _loss_streak(closed: list) -> int:
    """Count consecutive losing closed trades counting back from the most recent.

    Trades arrive ordered by exit_time ascending from storage.closed_trades(); we
    walk from the newest backwards and stop at the first non-loss. A trade is a
    loss when its realized pnl is strictly negative; flat/None ends the streak.
    """
    streak = 0
    for row in reversed(closed or []):
        try:
            pnl = row.get("pnl") if isinstance(row, dict) else None
        except Exception:  # noqa: BLE001
            pnl = None
        if pnl is None:
            break
        try:
            pnlf = float(pnl)
        except (TypeError, ValueError):
            break
        if pnlf < 0.0:
            streak += 1
        else:
            break
    return streak


def _rolling_expectancy(closed: list, window: int, stop_pct: float):
    """Mean R over the last `window` closed trades. R = pnl / risk, where
    risk = entry_price * stop_pct (the per-trade hard stop, read only, never set).

    Returns (mean_R, n_used). n_used is the count of trades that produced a usable
    R (positive entry price and positive stop). Returns (0.0, 0) when none usable.
    """
    if not closed or window <= 0 or stop_pct <= 0.0:
        return 0.0, 0
    recent = list(closed)[-int(window):]
    rs: list[float] = []
    for row in recent:
        if not isinstance(row, dict):
            continue
        entry = _safe_float(row.get("entry_price"), 0.0)
        qty = _safe_float(row.get("qty"), 0.0)
        pnl = row.get("pnl")
        if pnl is None or entry <= 0.0 or qty <= 0.0:
            continue
        pnlf = _safe_float(pnl, None) if pnl is not None else None
        if pnlf is None:
            continue
        # Risk in dollars for the whole position = entry * stop_pct * qty.
        risk = entry * stop_pct * qty
        if risk <= 0.0:
            continue
        rs.append(pnlf / risk)
    if not rs:
        return 0.0, 0
    return sum(rs) / len(rs), len(rs)


def _calibration(storage, cfg):
    """(calibration_error, n_samples). Fail safe to (None, 0) on any error.

    n_samples is the count of resolved (non-pending) outcomes that feed the
    calibration computation, used to gate the MISCALIBRATED alert.
    """
    err = None
    try:
        from ..learning.calibration import calibration_error
        err = float(calibration_error(storage, cfg))
    except Exception as exc:  # noqa: BLE001
        log.debug("calibration_error failed: %s", exc)
        err = None
    n = 0
    try:
        n = len(storage.resolved_outcomes() or [])
    except Exception as exc:  # noqa: BLE001
        log.debug("resolved_outcomes failed: %s", exc)
        n = 0
    return err, n


def _equity_and_stage(storage, cfg, md):
    """(equity, stage_start) from the engine ledger. (None, None) on failure."""
    try:
        from ..engine.state import EngineState
        eng = EngineState(storage, cfg)
        acct = eng.account_state(md)
        raw_equity = getattr(acct, "equity", None) if acct is not None else None
        equity = None if raw_equity is None else _safe_float(raw_equity, None)
        raw_stage = eng.stage_start_equity
        stage_start = None if raw_stage is None else _safe_float(raw_stage, None)
        return equity, stage_start
    except Exception as exc:  # noqa: BLE001
        log.debug("equity/stage lookup failed: %s", exc)
        return None, None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def assess_health(storage, cfg=None, md=None) -> dict:
    """Pure computation of the kid's aggregate financial health.

    Returns a dict with the metrics and a list of crossed-threshold alerts. Never
    raises: on any failure it returns a conservative report. healthy is True only
    when no alerts fired. Missing data does NOT manufacture alerts (it is noted in
    the "notes" list instead), so the guardian never false-positives on a cold DB.
    """
    notes: list[str] = []
    alerts: list[dict] = []

    cfg = _get_cfg(cfg)
    mcfg = _mother_cfg(cfg)

    # Thresholds (read-only; defaults match MotherConfig so a missing cfg is safe).
    drawdown_alert_pct = _safe_float(_attr(mcfg, "drawdown_alert_pct", 0.20), 0.20)
    loss_streak_alert = int(_safe_float(_attr(mcfg, "loss_streak_alert", 4), 4))
    min_edge_samples = int(_safe_float(_attr(mcfg, "min_edge_samples", 30), 30))
    calibration_alert = _safe_float(_attr(mcfg, "calibration_alert", 0.30), 0.30)
    rolling_window = int(_safe_float(_attr(mcfg, "rolling_window", 20), 20))

    # Per-trade stop fraction lives in the risk law. We READ it (never write it)
    # purely to turn dollar pnl into R for the expectancy metric.
    stop_pct = 0.08
    try:
        stop_pct = _safe_float(_attr(getattr(cfg, "risk", None),
                                     "per_trade_stop_pct", 0.08), 0.08)
    except Exception as exc:  # noqa: BLE001
        log.debug("per_trade_stop_pct read failed: %s", exc)

    # ----- equity / stage / drawdown -----
    equity, stage_start = _equity_and_stage(storage, cfg, md)
    peak_equity = _peak_equity(storage)

    drawdown_pct = 0.0
    if peak_equity is not None and peak_equity > 0.0 and equity is not None:
        drawdown_pct = max(0.0, (peak_equity - equity) / peak_equity)
        if drawdown_pct >= drawdown_alert_pct:
            alerts.append({
                "code": CODE_DRAWDOWN,
                "severity": "high",
                "message": (
                    "Drawdown %.1f%% from peak $%.2f (now $%.2f) at or past the "
                    "%.1f%% early-brake threshold. This sits ABOVE the hard ruin "
                    "floor; review process before it gets there."
                    % (drawdown_pct * 100.0, peak_equity, equity,
                       drawdown_alert_pct * 100.0)
                ),
            })
    else:
        notes.append("drawdown unknown (no equity ledger or no equity); not alerting")

    stage_pnl = 0.0
    if equity is not None and stage_start is not None:
        stage_pnl = equity - stage_start
    elif stage_start is None:
        notes.append("stage_start unknown")

    # ----- loss streak + rolling expectancy (from closed trades) -----
    try:
        closed = storage.closed_trades() or []
    except Exception as exc:  # noqa: BLE001
        log.debug("closed_trades failed: %s", exc)
        closed = []
        notes.append("closed_trades unavailable")

    loss_streak = _loss_streak(closed)
    if loss_streak >= loss_streak_alert:
        alerts.append({
            "code": CODE_LOSS_STREAK,
            "severity": "medium",
            "message": (
                "Loss streak of %d closed trades (alert at %d). Aggregate signal "
                "only; do not over-read any single loss."
                % (loss_streak, loss_streak_alert)
            ),
        })

    rolling_expectancy, rolling_n = _rolling_expectancy(closed, rolling_window, stop_pct)
    if rolling_n >= min_edge_samples and rolling_expectancy < 0.0:
        alerts.append({
            "code": CODE_NEG_EXPECTANCY,
            "severity": "medium",
            "message": (
                "Rolling expectancy %.3f R over last %d closed trades is negative "
                "with a meaningful sample (>= %d). The kid is losing edge, not just "
                "luck." % (rolling_expectancy, rolling_n, min_edge_samples)
            ),
        })
    elif rolling_n < min_edge_samples:
        notes.append(
            "expectancy sample %d below min_edge_samples %d; not alerting"
            % (rolling_n, min_edge_samples)
        )

    # ----- calibration -----
    calib_err, samples = _calibration(storage, cfg)
    calib_out = calib_err if calib_err is not None else 0.5
    if (calib_err is not None and samples >= min_edge_samples
            and calib_err >= calibration_alert):
        alerts.append({
            "code": CODE_MISCALIBRATED,
            "severity": "medium",
            "message": (
                "Calibration error %.3f over %d resolved outcomes is at or past the "
                "%.3f alert with a meaningful sample. The kid's stated confidence "
                "does not match reality." % (calib_err, samples, calibration_alert)
            ),
        })
    elif calib_err is None:
        notes.append("calibration unknown; not alerting")
    elif samples < min_edge_samples:
        notes.append(
            "calibration sample %d below min_edge_samples %d; not alerting"
            % (samples, min_edge_samples)
        )

    return {
        "stage_start": stage_start if stage_start is not None else 0.0,
        "equity": equity if equity is not None else 0.0,
        "peak_equity": peak_equity if peak_equity is not None else 0.0,
        "drawdown_pct": drawdown_pct,
        "stage_pnl": stage_pnl,
        "loss_streak": loss_streak,
        "rolling_expectancy": rolling_expectancy,
        "rolling_n": rolling_n,
        "calibration_error": calib_out,
        "samples": samples,
        "alerts": alerts,
        "healthy": len(alerts) == 0,
        "notes": notes,
    }


def check_and_alert(storage, cfg=None, md=None, notify: Optional[Callable] = None) -> dict:
    """Run assess_health, then fire `notify` ONLY for codes newly crossing from
    inactive to active. Codes that recovered since last cycle are cleared. The new
    active set is persisted to kv "mother.guardian_state" so a still-active code is
    not re-announced every cycle.

    If cfg.mother.auto_defensive is on and DRAWDOWN is among the NEW alerts, set kv
    "armed" to False (a soft brake) and record it in the result. The hard ruin law
    already handles true ruin; this only stops new fires early. Never raises.
    """
    try:
        assessment = assess_health(storage, cfg, md)
    except Exception as exc:  # noqa: BLE001
        log.debug("assess_health raised unexpectedly: %s", exc)
        assessment = {
            "stage_start": 0.0, "equity": 0.0, "peak_equity": 0.0, "drawdown_pct": 0.0,
            "stage_pnl": 0.0, "loss_streak": 0, "rolling_expectancy": 0.0,
            "rolling_n": 0, "calibration_error": 0.5, "samples": 0,
            "alerts": [], "healthy": True, "notes": ["assess_health error"],
        }

    # Current active codes (de-duplicated, order-stable).
    active_codes: list[str] = []
    by_code: dict[str, dict] = {}
    for a in assessment.get("alerts", []) or []:
        try:
            code = a.get("code")
        except Exception:  # noqa: BLE001
            code = None
        if code and code not in by_code:
            by_code[code] = a
            active_codes.append(code)
    active_set = set(active_codes)

    # Previously active codes from kv (guarded; treat anything odd as empty).
    prev: list[str] = []
    try:
        raw_prev = storage.kv_get(K_GUARDIAN_STATE, [])
        if isinstance(raw_prev, (list, tuple, set)):
            prev = [str(c) for c in raw_prev]
        elif isinstance(raw_prev, str) and raw_prev:
            prev = [raw_prev]
    except Exception as exc:  # noqa: BLE001
        log.debug("guardian_state read failed: %s", exc)
        prev = []
    prev_set = set(prev)

    new_codes = [c for c in active_codes if c not in prev_set]
    cleared = [c for c in prev if c not in active_set]

    new_alerts = [by_code[c] for c in new_codes if c in by_code]

    # Notify only on inactive -> active transitions.
    if notify is not None and callable(notify):
        for alert in new_alerts:
            try:
                msg = "[guardian] %s (%s): %s" % (
                    alert.get("code", "?"), alert.get("severity", "?"),
                    alert.get("message", ""),
                )
                notify(msg)
            except Exception as exc:  # noqa: BLE001
                log.debug("notify failed for %s: %s", alert.get("code"), exc)

    # Optional soft brake: disarm on a NEW drawdown alert if configured.
    auto_defensive_triggered = False
    note = ""
    try:
        cfg_obj = _get_cfg(cfg)
        mcfg = _mother_cfg(cfg_obj)
        auto_defensive = bool(_attr(mcfg, "auto_defensive", False))
    except Exception as exc:  # noqa: BLE001
        log.debug("auto_defensive read failed: %s", exc)
        auto_defensive = False
    if auto_defensive and CODE_DRAWDOWN in new_codes:
        try:
            storage.kv_set(K_ARMED, False)
            auto_defensive_triggered = True
            note = ("auto_defensive: NEW drawdown alert -> set armed=False (soft "
                    "early brake; the hard ruin law is unchanged; operator can re-arm)")
            if notify is not None and callable(notify):
                try:
                    notify("[guardian] " + note)
                except Exception as exc:  # noqa: BLE001
                    log.debug("notify failed for auto_defensive: %s", exc)
            log.info("guardian auto_defensive disarmed on new drawdown alert")
        except Exception as exc:  # noqa: BLE001
            log.debug("auto_defensive disarm failed: %s", exc)
            note = "auto_defensive intended to disarm but kv_set failed"

    # Persist the new active set so still-active codes do not re-alert next cycle.
    try:
        storage.kv_set(K_GUARDIAN_STATE, active_codes)
    except Exception as exc:  # noqa: BLE001
        log.debug("guardian_state persist failed: %s", exc)

    return {
        "assessment": assessment,
        "new_alerts": new_alerts,
        "cleared": cleared,
        "auto_defensive_triggered": auto_defensive_triggered,
        "note": note,
    }
