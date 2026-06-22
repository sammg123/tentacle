"""Graduation: the gate from PAPER to a REAL-money account.

The whole paper ladder ($0 -> $25 -> ... -> $1000) is the kid growing up. Going to
REAL money is a separate, momentous step that ONLY the mother can approve, "raising
him fully until he grows up." She approves only when he has truly proven himself:
top rung, sustained mastery, good calibration, positive forward edge over a large
sample, a healthy account, and her own trust intact.

Approval is necessary but not sufficient: the human must still point TENTACLE at a
real account (ALPACA_PAPER=false + live keys) and arm it. The engine refuses to place
a real-money order unless this approval flag is set, so neither the kid nor an
impatient operator can reach real money before the mother signs off.
"""
from __future__ import annotations

from typing import Optional

from ..config import Config, get_config
from ..util import get_logger, now_iso

log = get_logger("tentacle.mother.graduation")

GRADUATION_KEY = "mother.graduation_approved"
GRADUATION_AT = "mother.graduation_at"


def is_graduated(storage) -> bool:
    try:
        return bool(storage.kv_get(GRADUATION_KEY, False))
    except Exception:  # noqa: BLE001
        return False


def revoke_graduation(storage) -> None:
    try:
        storage.kv_set(GRADUATION_KEY, False)
        log.warning("graduation approval REVOKED; real-money trading is blocked again")
    except Exception:  # noqa: BLE001
        pass


def assess_graduation(storage, cfg: Optional[Config] = None, md=None) -> dict:
    """Evaluate whether the kid is ready for a real-money account.

    Sets the approval flag the first time ALL criteria are met. Returns a report
    with per-criterion booleans, the failing reasons, and whether this call newly
    granted approval. Never raises.
    """
    cfg = cfg or get_config()
    out = {"ready": False, "approved": is_graduated(storage), "newly_approved": False,
           "criteria": {}, "reasons": [], "mastery": 0.0, "samples": 0}
    try:
        from ..engine.state import EngineState
        from ..config import LADDER, rung_by_level
        from ..ladder.mastery import compute_mastery
        from ..learning.calibration import calibration_error
        from .guardian import assess_health

        state = EngineState(storage, cfg)
        rung = state.rung
        top = max(r.level for r in LADDER)
        crit = out["criteria"]
        reasons = out["reasons"]

        crit["alive"] = bool(state.alive)
        if not state.alive:
            reasons.append("the bot has DIED and must re-qualify from a lower rung")

        crit["at_top_rung"] = (rung >= top)
        if rung < top:
            reasons.append(f"not at the top paper rung yet ({rung}/{top})")

        mastery, _ = compute_mastery(storage, cfg)
        out["mastery"] = round(float(mastery), 1)
        top_bar = float(rung_by_level(top).promote_mastery)
        crit["mastery_ok"] = (mastery >= top_bar)
        if mastery < top_bar:
            reasons.append(f"mastery {mastery:.0f} < required {top_bar:.0f}")

        cal = float(calibration_error(storage, cfg))
        crit["calibration_ok"] = (cal < cfg.mother.well_calibration)
        if cal >= cfg.mother.well_calibration:
            reasons.append(f"calibration error {cal:.2f} >= {cfg.mother.well_calibration}")

        health = assess_health(storage, cfg, md) or {}
        n = int(health.get("samples", 0) or 0)
        out["samples"] = n
        # Real money demands a lot of forward evidence, not just a passing grade.
        min_live = max(100, int(cfg.mother.min_edge_samples) * 3)
        crit["enough_samples"] = (n >= min_live)
        if n < min_live:
            reasons.append(f"only {n} resolved signals (need >= {min_live})")

        edge = float(health.get("rolling_expectancy", 0.0) or 0.0)
        crit["positive_edge"] = (edge > 0)
        if edge <= 0:
            reasons.append(f"rolling expectancy {edge:.3f} is not positive")

        crit["healthy"] = bool(health.get("healthy", False))
        if not health.get("healthy", False):
            reasons.append("the financial guardian is not healthy (drawdown / streak / calibration alert)")

        trust = float(storage.kv_get("mother.trust", 1.0) or 1.0)
        crit["trust_ok"] = (trust >= 1.0)
        if trust < 1.0:
            reasons.append(f"the mother's self-trust is {trust:.2f} (< 1.0); her judgment is on probation")

        ready = all(crit.values())
        out["ready"] = ready
        already = is_graduated(storage)
        if ready and not already:
            storage.kv_set(GRADUATION_KEY, True)
            storage.kv_set(GRADUATION_AT, now_iso())
            out["newly_approved"] = True
            log.warning("MOTHER APPROVES graduation: the kid has proven itself. "
                        "Real-money trading is unlocked (operator must still provide "
                        "live keys and arm).")
        out["approved"] = is_graduated(storage)
        return out
    except Exception as exc:  # noqa: BLE001
        out["reasons"].append(f"assessment error: {exc}")
        return out
