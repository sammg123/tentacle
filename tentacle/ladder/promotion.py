"""Ladder promotion / demotion / death.

The bot lives at a rung (stored in kv "rung", default -1). Each evaluation:

  1. Compute current Mastery + the qualifying-sample count.
  2. DEATH first: if the stage equity has fallen through the ruin floor
     (equity <= stage_start * (1 - ruin_floor_pct)), the bot has DIED. We set kv
     "alive" False, DEMOTE one rung, reset stage_start_equity to the LOWER rung's
     capital, and force re-qualification from there. We never silently self-restart
     at the same size after a ruin.
  3. PROMOTE: if mastery >= the rung's promote_mastery AND samples >= promote_samples,
     advance one rung, persist kv "rung", and reset stage_start_equity to the new
     rung's capital.
  4. Otherwise HOLD.

Everything is read from storage so the decision survives a restart. The ruin check
uses the same equity derivation the risk manager uses (stage_start + realized +
unrealized P&L). Unknown prices make unrealized P&L 0 for that position (FAIL SAFE:
we do not invent gains, and a missing price never fabricates a promotion).
"""
from __future__ import annotations

from typing import Optional

from ..config import Config, get_config, rung_by_level
from ..util import get_logger

log = get_logger("tentacle.ladder.promotion")


def evaluate_ladder(storage, cfg: Optional[Config] = None, md=None) -> dict:
    """Decide hold / promote / demote / death for the current rung.

    Returns {"rung", "mastery", "samples", "action", "new_rung", "note"}. Side effects
    (kv writes) only happen on promote / demote / death. On any unexpected error we
    return a safe HOLD so a learning hiccup never advances or kills the bot by accident.
    """
    cfg = cfg or get_config()

    if getattr(cfg.engine, "no_ladder", False):
        # Ladder disabled: never promote, demote, or die. Fixed full-capital paper trading.
        lvl = _as_int(storage.kv_get("rung", 4), 4)
        return {"rung": lvl, "mastery": 0.0, "samples": 0, "action": "hold",
                "new_rung": lvl, "note": "ladder disabled (TENTACLE_NO_LADDER)"}

    try:
        cur_level = _as_int(storage.kv_get("rung", -1), -1)
    except Exception as exc:  # noqa: BLE001
        log.debug("kv_get rung failed: %s", exc)
        cur_level = -1

    cur_rung = rung_by_level(cur_level)

    # ----------------------------------------------------------- mastery + samples
    try:
        from .mastery import compute_mastery
        mastery, _components = compute_mastery(storage, cfg, rung=cur_rung)
    except Exception as exc:  # noqa: BLE001
        log.debug("compute_mastery failed: %s", exc)
        mastery = 0.0
    samples = _qualifying_samples(storage)

    result = {
        "rung": cur_level,
        "mastery": round(float(mastery), 3),
        "samples": samples,
        "action": "hold",
        "new_rung": cur_level,
        "note": "",
    }

    # ----------------------------------------------------------- DEATH (ruin floor)
    try:
        equity, stage_start = _stage_equity(storage, cfg, md)
        if _ruin_breached(equity, stage_start, cfg):
            new_level = max(_lowest_level(cfg), cur_level - 1)
            new_rung = rung_by_level(new_level)
            try:
                storage.kv_set("alive", False)
                storage.kv_set("rung", new_level)
                storage.kv_set("stage_start_equity", float(new_rung.capital))
                storage.kv_set("halted_today", True)
            except Exception as exc:  # noqa: BLE001
                log.debug("death kv writes failed: %s", exc)
            result.update(
                action="death",
                new_rung=new_level,
                note=(
                    f"RUIN: equity {equity:.2f} <= floor "
                    f"{stage_start * (1 - cfg.risk.ruin_floor_pct):.2f} of stage start "
                    f"{stage_start:.2f}. Demoted to rung {new_level} "
                    f"({new_rung.name}); must re-qualify. alive=False."
                ),
            )
            log.warning("DEATH at rung %s -> demote to %s", cur_level, new_level)
            return result
    except Exception as exc:  # noqa: BLE001
        log.debug("ruin check failed (treated as no death): %s", exc)

    # ----------------------------------------------------------- PROMOTE
    try:
        if (
            mastery >= float(cur_rung.promote_mastery)
            and samples >= int(cur_rung.promote_samples)
        ):
            top_level = _highest_level(cfg)
            if cur_level >= top_level:
                result["note"] = (
                    f"mastery {mastery:.1f} and {samples} samples clear the bar, but "
                    f"already at the top rung ({cur_rung.name}). Holding at full size."
                )
                return result
            new_level = cur_level + 1
            new_rung = rung_by_level(new_level)
            try:
                storage.kv_set("rung", new_level)
                storage.kv_set("stage_start_equity", float(new_rung.capital))
            except Exception as exc:  # noqa: BLE001
                log.debug("promotion kv writes failed: %s", exc)
            result.update(
                action="promote",
                new_rung=new_level,
                note=(
                    f"PROMOTE: mastery {mastery:.1f} >= {cur_rung.promote_mastery} and "
                    f"{samples} >= {cur_rung.promote_samples} samples. "
                    f"rung {cur_level} -> {new_level} ({new_rung.name}, "
                    f"${new_rung.capital:.0f})."
                ),
            )
            log.info("PROMOTE rung %s -> %s", cur_level, new_level)
            return result
    except Exception as exc:  # noqa: BLE001
        log.debug("promotion check failed: %s", exc)

    # ----------------------------------------------------------- HOLD
    result["note"] = (
        f"HOLD at rung {cur_level} ({cur_rung.name}): mastery {mastery:.1f}/"
        f"{cur_rung.promote_mastery}, samples {samples}/{cur_rung.promote_samples}."
    )
    return result


# --------------------------------------------------------------------------- #
# Helpers (kept local; no dependency on risk/* which this package does not own)
# --------------------------------------------------------------------------- #
def _qualifying_samples(storage) -> int:
    """Number of resolved outcomes in the current record (the qualifying count).

    Outcomes (fired + shadow) are the unit of evidence the ladder counts: they are
    what mastery is computed over, so the sample bar is measured on the same set.
    """
    try:
        from ..models import OutcomeLabel
        return sum(
            1 for o in storage.resolved_outcomes()
            if o.get("label") != OutcomeLabel.PENDING.value
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("_qualifying_samples failed: %s", exc)
        return 0


def _stage_equity(storage, cfg: Config, md=None) -> tuple[float, float]:
    """Derive (equity, stage_start) the way the risk manager does.

    stage_start = kv "stage_start_equity" if set, else the current rung's capital.
    equity = stage_start + realized P&L (closed trades this stage) + unrealized P&L
    (open trades priced via md). A missing price contributes 0 unrealized for that
    position (we never invent P&L). Returns (equity, stage_start).
    """
    cur_level = _as_int(storage.kv_get("rung", -1), -1)
    cur_rung = rung_by_level(cur_level)

    stage_start = storage.kv_get("stage_start_equity", None)
    try:
        stage_start = float(stage_start) if stage_start is not None else float(cur_rung.capital)
    except (TypeError, ValueError):
        stage_start = float(cur_rung.capital)

    realized = 0.0
    try:
        for t in storage.closed_trades():
            pnl = t.get("pnl")
            if pnl is not None:
                try:
                    realized += float(pnl)
                except (TypeError, ValueError):
                    continue
    except Exception as exc:  # noqa: BLE001
        log.debug("closed_trades failed: %s", exc)

    unrealized = 0.0
    if md is not None:
        try:
            for t in storage.open_trades():
                try:
                    qty = float(t.get("qty") or 0.0)
                    entry = float(t.get("entry_price") or 0.0)
                    ticker = t.get("ticker")
                except (TypeError, ValueError):
                    continue
                if not ticker or qty <= 0 or entry <= 0:
                    continue
                price = None
                try:
                    price = md.last_price(ticker)
                except Exception:  # noqa: BLE001
                    price = None
                if price is None:
                    # FAIL SAFE: unknown price contributes no unrealized P&L.
                    continue
                try:
                    unrealized += (float(price) - entry) * qty
                except (TypeError, ValueError):
                    continue
        except Exception as exc:  # noqa: BLE001
            log.debug("open_trades unrealized failed: %s", exc)

    equity = stage_start + realized + unrealized
    return equity, stage_start


def _ruin_breached(equity: float, stage_start: float, cfg: Config) -> bool:
    """True when equity has fallen through the stage ruin floor.

    Mirrors risk.caps.ruin_breached: equity <= stage_start * (1 - ruin_floor_pct).
    A non-positive or unknown stage_start cannot trigger a death (nothing to ruin),
    which keeps a fresh $0 backtest rung from "dying".
    """
    try:
        if stage_start is None or stage_start <= 0:
            return False
        floor = stage_start * (1.0 - float(cfg.risk.ruin_floor_pct))
        return float(equity) <= floor
    except Exception:  # noqa: BLE001
        return False


def _lowest_level(cfg: Config) -> int:
    levels = [r.level for r in cfg.ladder] if getattr(cfg, "ladder", None) else []
    return min(levels) if levels else -1


def _highest_level(cfg: Config) -> int:
    levels = [r.level for r in cfg.ladder] if getattr(cfg, "ladder", None) else []
    return max(levels) if levels else 4


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
