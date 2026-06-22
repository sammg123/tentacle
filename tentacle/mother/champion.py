"""Champion / challenger promotion + meta-calibration (the mother's third eye).

The mother never edits the live kid directly. Eye 1 (the blind process review)
writes a CHALLENGER policy. This module decides, conservatively, whether that
challenger should be PROMOTED to champion, and afterward audits whether her past
promotions actually helped (meta-calibration), nudging a trust score that makes
her demand a larger margin when she has been unhelpful.

Promotion gates (ALL must hold):
  - a challenger exists (policy.get_challenger_policy),
  - the cooldown since the last promotion has elapsed,
  - the champion is NOT already "performing well" (do not fix a winner),
  - the challenger beats the champion by >= cfg.mother.promotion_margin on a
    blind, decision-time, outcome-free comparison, AND
  - the mother's trust score allows it.

HARD BOUNDARY: this module only reads thresholds from config and only edits the
reasoning policy via policy.promote_challenger. It NEVER touches sizing, stops,
the ruin floor, the daily halt, the ladder caps, or the settled-cash rule. It
reads cfg.risk values only to compute risk-normalized R for expectancy, never to
change them. Everything is guarded; nothing here ever raises.

Honesty note: an LLM critiquing an LLM can share blind spots, and a higher
process score is not proof of live edge. The real validation is forward, in
shadow. v1 builds the full structure and gates promotion conservatively.
"""
from __future__ import annotations

from typing import Optional

from ..util import get_logger, now_iso, parse_dt

log = get_logger("tentacle.mother.champion")

# kv keys (mirror policy.py + the design doc).
K_LAST_PROMO = "policy.last_promotion_at"
K_TRUST = "mother.trust"

# Trust score bounds and step (the mother demands a bigger margin when low trust).
_TRUST_MIN = 0.3
_TRUST_MAX = 1.5
_TRUST_DEFAULT = 1.0
_TRUST_STEP = 0.1

# How old (hours) a promotion must be before meta-calibration will judge it, and
# how many resolved samples we want before the judgement is meaningful.
_META_MIN_AGE_HOURS = 168.0   # ~one week, matches the weekly promotion cadence
_META_MIN_SAMPLES = 10


# --------------------------------------------------------------------------- #
# Small guarded helpers
# --------------------------------------------------------------------------- #
def _get_config(cfg):
    if cfg is not None:
        return cfg
    try:
        from ..config import get_config
        return get_config()
    except Exception as exc:  # noqa: BLE001
        log.debug("get_config failed: %s", exc)
        return None


def _get_storage(storage):
    if storage is not None:
        return storage
    try:
        from ..storage import get_storage
        return get_storage()
    except Exception as exc:  # noqa: BLE001
        log.debug("get_storage failed: %s", exc)
        return None


def _f(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _mother_cfg(cfg):
    """Return the MotherConfig, tolerating a missing attribute."""
    return getattr(cfg, "mother", None) if cfg is not None else None


def _risk_stop_pct(cfg) -> float:
    """The per-trade stop fraction, used ONLY to risk-normalize R. Read-only."""
    try:
        risk = getattr(cfg, "risk", None)
        pct = _f(getattr(risk, "per_trade_stop_pct", 0.08), 0.08)
        return pct if pct > 0 else 0.08
    except Exception:  # noqa: BLE001
        return 0.08


def _get_trust(storage) -> float:
    try:
        v = storage.kv_get(K_TRUST, _TRUST_DEFAULT)
        t = _f(v, _TRUST_DEFAULT)
        return max(_TRUST_MIN, min(_TRUST_MAX, t))
    except Exception:  # noqa: BLE001
        return _TRUST_DEFAULT


def _set_trust(storage, value: float) -> float:
    t = max(_TRUST_MIN, min(_TRUST_MAX, _f(value, _TRUST_DEFAULT)))
    try:
        storage.kv_set(K_TRUST, t)
    except Exception as exc:  # noqa: BLE001
        log.debug("kv_set trust failed: %s", exc)
    return t


# --------------------------------------------------------------------------- #
# Metrics (calibration / expectancy / process), all guarded
# --------------------------------------------------------------------------- #
def _calibration_error(storage, cfg) -> float:
    try:
        from ..learning.calibration import calibration_error
        return _f(calibration_error(storage, cfg), 0.5)
    except Exception as exc:  # noqa: BLE001
        log.debug("calibration_error failed: %s", exc)
        return 0.5


def _expectancy(storage, cfg) -> tuple[float, int]:
    """Mean R over closed trades. R = pnl / risk, risk = entry * stop_pct.

    Returns (mean_R, n). On any trouble returns (0.0, 0). Read-only over the
    closed_trades journal; cfg.risk is read only to normalize, never modified.
    """
    try:
        rows = storage.closed_trades()
    except Exception as exc:  # noqa: BLE001
        log.debug("closed_trades failed: %s", exc)
        return 0.0, 0
    if not rows:
        return 0.0, 0
    stop_pct = _risk_stop_pct(cfg)
    rs: list[float] = []
    for r in rows:
        try:
            pnl = r.get("pnl")
            if pnl is None:
                continue
            entry = _f(r.get("entry_price"), 0.0)
            qty = _f(r.get("qty"), 0.0)
            # Prefer an explicit stop distance when present; else entry * stop_pct.
            stop_price = r.get("stop_price")
            if stop_price is not None and entry > 0:
                risk_per_share = abs(entry - _f(stop_price, 0.0))
            else:
                risk_per_share = entry * stop_pct
            if risk_per_share <= 0 or entry <= 0:
                continue
            # Total dollars at risk approximated by per-share risk times quantity.
            risk_dollars = risk_per_share * qty if qty > 0 else risk_per_share
            if risk_dollars <= 0:
                continue
            rs.append(_f(pnl, 0.0) / risk_dollars)
        except Exception:  # noqa: BLE001
            continue
    if not rs:
        return 0.0, 0
    return sum(rs) / len(rs), len(rs)


def _recent_review(storage, cfg) -> dict:
    """Run Eye 1 (blind, outcome-free) if available. Safe empty dict otherwise."""
    try:
        from . import review as _review
    except Exception as exc:  # noqa: BLE001
        log.debug("review module unavailable: %s", exc)
        return {}
    try:
        out = _review.review_decisions(storage, cfg)
        return out if isinstance(out, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.debug("review_decisions failed: %s", exc)
        return {}


def _metrics_snapshot(storage, cfg) -> dict:
    """A calibration / expectancy / process snapshot for the change log."""
    calib = _calibration_error(storage, cfg)
    expectancy, n = _expectancy(storage, cfg)
    review = _recent_review(storage, cfg)
    return {
        "calibration_error": round(calib, 4),
        "expectancy": round(expectancy, 4),
        "expectancy_n": n,
        "avg_process_score": _f(review.get("avg_process_score"), 0.0),
        "process_n": int(_f(review.get("n_reviewed"), 0)),
        "at": now_iso(),
    }


# --------------------------------------------------------------------------- #
# Blind, outcome-free holdout scoring under a given policy's lessons
# --------------------------------------------------------------------------- #
def _decision_base_score(row: dict) -> float:
    """A deterministic, outcome-free quality score (0..100) for one decision.

    Mirrors the spirit of Eye 1's heuristic fallback: reward decisions that cite
    real evidence from multiple families, that are timely, and whose reason flags
    show confirmation and trap-avoidance. NEVER looks at win/loss/pnl/exit.
    """
    import json as _json
    score = 50.0
    try:
        # Multiple independent families -> more coherent confirmation.
        fams = row.get("families")
        if isinstance(fams, str):
            try:
                fams = _json.loads(fams)
            except Exception:  # noqa: BLE001
                fams = []
        n_fams = len(fams) if isinstance(fams, (list, set, tuple)) else 0
        score += min(n_fams, 3) * 6.0

        # Evidence actually present and grounded.
        ev = row.get("evidence")
        if isinstance(ev, str):
            try:
                ev = _json.loads(ev)
            except Exception:  # noqa: BLE001
                ev = []
        n_ev = len(ev) if isinstance(ev, (list, tuple)) else 0
        score += min(n_ev, 4) * 3.0

        # A written thesis of substance.
        thesis = row.get("thesis") or ""
        if isinstance(thesis, str) and len(thesis.strip()) >= 40:
            score += 6.0

        # Reason flags: reward confirmation/freshness, penalize chase/stale traps.
        reasons = row.get("reasons")
        if isinstance(reasons, str):
            try:
                reasons = _json.loads(reasons)
            except Exception:  # noqa: BLE001
                reasons = [reasons]
        if isinstance(reasons, (list, tuple)):
            blob = " ".join(str(x).lower() for x in reasons)
        else:
            blob = str(reasons or "").lower()
        for good in ("confirm", "fresh", "multi", "primary", "independent"):
            if good in blob:
                score += 2.0
        for bad in ("chase", "stale", "single", "echo", "pump", "late"):
            if bad in blob:
                score -= 4.0
    except Exception:  # noqa: BLE001
        return 50.0
    return max(0.0, min(100.0, score))


def _lessons_adjustment(rows: list, lessons: list) -> float:
    """Approximate how much a set of lessons would lift the batch's process score.

    We CANNOT truly re-run the kid under new lessons offline, so this is an
    honest approximation: each lesson that targets a trap visible in the batch's
    reason flags earns a small, capped credit. Returned reason strings are honest
    about this being an approximation.
    """
    if not lessons or not rows:
        return 0.0
    import json as _json
    # Build a frequency map of trap keywords present across the batch.
    trap_hits = 0
    total = 0
    for row in rows:
        total += 1
        reasons = row.get("reasons")
        if isinstance(reasons, str):
            try:
                reasons = _json.loads(reasons)
            except Exception:  # noqa: BLE001
                reasons = [reasons]
        if isinstance(reasons, (list, tuple)):
            blob = " ".join(str(x).lower() for x in reasons)
        else:
            blob = str(reasons or "").lower()
        if any(b in blob for b in ("chase", "stale", "single", "echo", "pump", "late")):
            trap_hits += 1
    if total == 0:
        return 0.0
    trap_rate = trap_hits / total
    lesson_blob = " ".join(str(le).lower() for le in lessons)
    targeted = sum(
        1 for b in ("chase", "stale", "single", "echo", "pump", "late",
                    "confirm", "primary", "independent", "fresh")
        if b in lesson_blob
    )
    # A lesson can only help where the batch actually shows the flaw. Cap the lift.
    lift = trap_rate * min(targeted, 3) * 4.0
    return max(0.0, min(lift, 12.0))


def _score_holdout(storage, cfg, lessons: list) -> tuple[float, int]:
    """Mean blind base score over a holdout of recent decisions, plus a lessons
    adjustment. Returns (score, n_decisions). (0.0, 0) when nothing to score."""
    try:
        mcfg = _mother_cfg(cfg)
        limit = int(_f(getattr(mcfg, "review_batch", 20), 20)) if mcfg else 20
        rows = storage.recent_decisions(limit, with_outcomes=False)
    except Exception as exc:  # noqa: BLE001
        log.debug("recent_decisions failed: %s", exc)
        return 0.0, 0
    if not rows:
        return 0.0, 0
    base_scores = [_decision_base_score(r) for r in rows]
    base = sum(base_scores) / len(base_scores)
    base += _lessons_adjustment(rows, lessons or [])
    return max(0.0, min(100.0, base)), len(rows)


# --------------------------------------------------------------------------- #
# "Performing well" test (anti-overcorrection: do not fix a winner)
# --------------------------------------------------------------------------- #
def _champion_performing_well(storage, cfg, mcfg) -> tuple[bool, str]:
    """Healthy guardian AND well-calibrated AND a recent high process score."""
    # Guardian health (optional sibling module; absence does not block the test).
    healthy = None
    try:
        from . import guardian as _guardian
        assessment = _guardian.assess_health(storage, cfg)
        if isinstance(assessment, dict):
            healthy = bool(assessment.get("healthy"))
    except Exception as exc:  # noqa: BLE001
        log.debug("guardian.assess_health unavailable: %s", exc)
        healthy = None

    well_calib = _f(getattr(mcfg, "well_calibration", 0.15), 0.15) if mcfg else 0.15
    well_proc = _f(getattr(mcfg, "well_process_score", 80.0), 80.0) if mcfg else 80.0
    min_samples = int(_f(getattr(mcfg, "min_edge_samples", 30), 30)) if mcfg else 30

    calib = _calibration_error(storage, cfg)
    review = _recent_review(storage, cfg)
    avg_proc = _f(review.get("avg_process_score"), 0.0)
    proc_n = int(_f(review.get("n_reviewed"), 0))

    # Require ENOUGH samples before we declare a winner; thin data is not "well".
    enough = proc_n >= min(min_samples, max(proc_n, 1)) and proc_n > 0

    well = (
        (healthy is True)
        and (calib < well_calib)
        and (avg_proc >= well_proc)
        and enough
    )
    detail = (
        "healthy=%s calib=%.3f(<%.3f) process=%.1f(>=%.1f,n=%d)"
        % (healthy, calib, well_calib, avg_proc, well_proc, proc_n)
    )
    return well, detail


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def evaluate_promotion(storage, cfg=None, md=None) -> dict:
    """Decide whether to promote the stored challenger to champion.

    Returns {"action": "promote"|"hold"|"reject", "reason": str,
             "champion_score": float|None, "challenger_score": float|None}.
    Never raises; HOLD is the safe default.
    """
    result = {"action": "hold", "reason": "", "champion_score": None,
              "challenger_score": None}
    storage = _get_storage(storage)
    if storage is None:
        result["reason"] = "no storage"
        return result
    cfg = _get_config(cfg)
    mcfg = _mother_cfg(cfg)

    try:
        # Gate 1: a challenger must exist.
        try:
            from .. import policy as _policy
            challenger = _policy.get_challenger_policy(storage)
            champion = _policy.get_champion_policy(storage)
        except Exception as exc:  # noqa: BLE001
            log.debug("policy access failed: %s", exc)
            result["reason"] = "policy module unavailable"
            return result
        if not isinstance(challenger, dict):
            result["reason"] = "no challenger to promote"
            return result

        # Gate 2: cooldown since the last promotion must have elapsed.
        cooldown_h = _f(getattr(mcfg, "promotion_cooldown_hours", 168.0), 168.0) if mcfg else 168.0
        last_at = None
        try:
            last_at = storage.kv_get(K_LAST_PROMO)
        except Exception:  # noqa: BLE001
            last_at = None
        if last_at:
            dt = parse_dt(last_at)
            if dt is not None:
                from ..util import now_utc
                elapsed_h = (now_utc() - dt).total_seconds() / 3600.0
                if elapsed_h < cooldown_h:
                    result["reason"] = (
                        "cooldown not elapsed (%.1fh of %.1fh)" % (elapsed_h, cooldown_h)
                    )
                    return result

        # Gate 3: do not fix a winner.
        well, detail = _champion_performing_well(storage, cfg, mcfg)
        if well:
            result["reason"] = "champion performing well, leaving him alone: " + detail
            return result

        trust = _get_trust(storage)

        # Gate 4 (PREFERRED): forward shadow A/B on real outcomes. Both arms judged
        # the same candidates and inherited the same outcomes, so this is a true,
        # market-direction-controlled edge comparison, not a rubric.
        ab = None
        try:
            from .abtest import forward_ab_compare
            ab = forward_ab_compare(storage, cfg)
        except Exception as exc:  # noqa: BLE001
            log.debug("forward_ab_compare failed: %s", exc)
            ab = None
        min_ab = int(_f(getattr(mcfg, "min_ab_samples", 12), 12)) if mcfg else 12

        if ab and int(ab.get("n", 0)) >= min_ab:
            champ_score = _f(ab.get("champion_edge"))
            chall_score = _f(ab.get("challenger_edge"))
            n = int(ab.get("n", 0))
            result["champion_score"] = round(champ_score, 5)
            result["challenger_score"] = round(chall_score, 5)
            result["ab"] = ab
            base_margin = _f(getattr(mcfg, "ab_edge_margin", 0.005), 0.005) if mcfg else 0.005
            required_margin = base_margin / max(trust, _TRUST_MIN)
            observed = chall_score - champ_score
            basis = ("forward shadow A/B over %d shared resolved candidates "
                     "(mean realized drift captured per arm)" % n)
        else:
            # Fallback: blind, outcome-free process-score holdout (weaker, see note).
            champ_lessons = champion.get("lessons") if isinstance(champion, dict) else []
            chall_lessons = challenger.get("lessons") if isinstance(challenger, dict) else []
            champ_score, n_champ = _score_holdout(storage, cfg, champ_lessons)
            chall_score, n_chall = _score_holdout(storage, cfg, chall_lessons)
            result["champion_score"] = round(champ_score, 2)
            result["challenger_score"] = round(chall_score, 2)
            review = _recent_review(storage, cfg)
            review_proc = _f(review.get("avg_process_score"), 0.0)
            review_n = int(_f(review.get("n_reviewed"), 0))
            if review_n > 0 and review_proc > 0:
                champ_score = (champ_score + review_proc) / 2.0
                result["champion_score"] = round(champ_score, 2)
            n = min(n_champ, n_chall)
            if n <= 0:
                ab_n = int((ab or {}).get("n", 0))
                result["reason"] = (
                    "not enough evidence to judge the challenger yet: %d/%d forward "
                    "A/B pairs and no holdout decisions; holding" % (ab_n, min_ab)
                )
                return result
            base_margin = _f(getattr(mcfg, "promotion_margin", 10.0), 10.0) if mcfg else 10.0
            required_margin = base_margin / max(trust, _TRUST_MIN)
            observed = chall_score - champ_score
            basis = ("approximate blind process-score holdout over %d decisions; "
                     "%d/%d forward A/B pairs so far (real validation is forward "
                     "in shadow)" % (n, int((ab or {}).get("n", 0)), min_ab))

        if observed < required_margin:
            result["action"] = "reject"
            result["reason"] = (
                "challenger does not beat champion by the required margin: "
                "observed %.4f < required %.4f (trust %.2f); basis: %s"
                % (observed, required_margin, trust, basis)
            )
            return result

        # All gates pass -> PROMOTE.
        metrics_before = _metrics_snapshot(storage, cfg)
        version = int(_f(challenger.get("version"), 0))
        summary = (
            "promote policy v%d: challenger %.4f vs champion %.4f (margin %.4f >= %.4f); "
            "basis: %s; %s"
            % (version, chall_score, champ_score, observed, required_margin, basis,
               (challenger.get("changelog") or "").strip()[:120])
        )

        promoted = None
        try:
            promoted = _policy.promote_challenger(storage)
        except Exception as exc:  # noqa: BLE001
            log.debug("promote_challenger failed: %s", exc)
            promoted = None
        if not promoted:
            result["action"] = "hold"
            result["reason"] = "promotion would qualify but promote_challenger failed"
            return result

        # Record the change with a metrics snapshot for later meta-calibration.
        try:
            storage.save_strategy_change(
                version=int(_f(promoted.get("version"), version)),
                kind="promote",
                summary=summary,
                metrics_before=metrics_before,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("save_strategy_change failed: %s", exc)

        # Stamp the promotion time (promote_challenger already sets it, but make
        # sure the key is fresh and ISO-formatted from our clock).
        try:
            storage.kv_set(K_LAST_PROMO, now_iso())
        except Exception as exc:  # noqa: BLE001
            log.debug("kv_set last_promotion failed: %s", exc)

        result["action"] = "promote"
        result["reason"] = summary
        log.info("evaluate_promotion: PROMOTED v%d", int(_f(promoted.get("version"), version)))
        return result
    except Exception as exc:  # noqa: BLE001
        log.debug("evaluate_promotion guarded failure: %s", exc)
        result["action"] = "hold"
        result["reason"] = "guarded failure: %s" % exc
        return result


def meta_calibration(storage, cfg=None, md=None) -> dict:
    """Audit past promotions: did the mother's changes actually help?

    For each strategy_changes row that lacks metrics_after and is old enough to
    judge, compute metrics_after, decide improved=bool vs metrics_before, finalize
    the row, and nudge kv "mother.trust" within [0.3, 1.5] (down when changes did
    not help, up when they did). Returns a summary dict. Never raises.
    """
    summary = {
        "measured": 0, "improved": 0, "regressed": 0, "skipped": 0,
        "trust_before": _TRUST_DEFAULT, "trust_after": _TRUST_DEFAULT,
        "details": [],
    }
    storage = _get_storage(storage)
    if storage is None:
        summary["reason"] = "no storage"
        return summary
    cfg = _get_config(cfg)

    try:
        trust = _get_trust(storage)
        summary["trust_before"] = round(trust, 3)

        try:
            pending = storage.unmeasured_changes()
        except Exception as exc:  # noqa: BLE001
            log.debug("unmeasured_changes failed: %s", exc)
            pending = []
        if not pending:
            summary["trust_after"] = round(trust, 3)
            summary["reason"] = "no unmeasured changes"
            return summary

        from ..util import now_utc
        net_delta = 0.0

        for row in pending:
            try:
                change_id = row.get("id")
                if change_id is None:
                    summary["skipped"] += 1
                    continue

                # Old enough to judge?
                created = parse_dt(row.get("created_at"))
                if created is not None:
                    age_h = (now_utc() - created).total_seconds() / 3600.0
                    if age_h < _META_MIN_AGE_HOURS:
                        summary["skipped"] += 1
                        continue

                before = _parse_metrics(row.get("metrics_before"))
                after = _metrics_snapshot(storage, cfg)

                # Need enough resolved samples before a judgement is meaningful.
                if int(_f(after.get("expectancy_n"), 0)) < _META_MIN_SAMPLES \
                        and int(_f(after.get("process_n"), 0)) < _META_MIN_SAMPLES:
                    summary["skipped"] += 1
                    continue

                improved = _is_improved(before, after)

                try:
                    storage.finalize_change(int(change_id), after, improved)
                except Exception as exc:  # noqa: BLE001
                    log.debug("finalize_change failed for %s: %s", change_id, exc)
                    summary["skipped"] += 1
                    continue

                summary["measured"] += 1
                if improved:
                    summary["improved"] += 1
                    net_delta += _TRUST_STEP
                else:
                    summary["regressed"] += 1
                    net_delta -= _TRUST_STEP
                summary["details"].append({
                    "id": int(change_id),
                    "kind": row.get("kind"),
                    "improved": bool(improved),
                    "before": before,
                    "after": after,
                })
            except Exception as exc:  # noqa: BLE001
                log.debug("meta_calibration row failed: %s", exc)
                summary["skipped"] += 1
                continue

        if net_delta != 0.0:
            trust = _set_trust(storage, trust + net_delta)
        summary["trust_after"] = round(trust, 3)
        return summary
    except Exception as exc:  # noqa: BLE001
        log.debug("meta_calibration guarded failure: %s", exc)
        summary["reason"] = "guarded failure: %s" % exc
        return summary


# --------------------------------------------------------------------------- #
# meta-calibration helpers
# --------------------------------------------------------------------------- #
def _parse_metrics(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        import json
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _is_improved(before: dict, after: dict) -> bool:
    """A promotion helped if, on balance, calibration got better (lower error),
    expectancy did not get worse, and the process score held or rose.

    Missing fields are treated neutrally so a sparse snapshot never falsely
    convicts (or absolves) the mother.
    """
    try:
        votes = 0

        b_cal = before.get("calibration_error")
        a_cal = after.get("calibration_error")
        if b_cal is not None and a_cal is not None:
            if _f(a_cal) < _f(b_cal) - 0.005:
                votes += 1
            elif _f(a_cal) > _f(b_cal) + 0.005:
                votes -= 1

        b_exp = before.get("expectancy")
        a_exp = after.get("expectancy")
        if b_exp is not None and a_exp is not None:
            if _f(a_exp) > _f(b_exp) + 0.02:
                votes += 1
            elif _f(a_exp) < _f(b_exp) - 0.02:
                votes -= 1

        b_proc = before.get("avg_process_score")
        a_proc = after.get("avg_process_score")
        if b_proc is not None and a_proc is not None:
            if _f(a_proc) > _f(b_proc) + 1.0:
                votes += 1
            elif _f(a_proc) < _f(b_proc) - 1.0:
                votes -= 1

        return votes > 0
    except Exception:  # noqa: BLE001
        return False
