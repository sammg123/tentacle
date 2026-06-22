"""Confidence: turn a Candidate's signal bundle into a 0..100 score.

Implements the EXACT formula in CONTRACTS.md (brain/confidence):

  group signals by family; per family f:
    best_f = max(s.raw.base_reliability * s.raw.signal_strength for s in f)
    rest_f = sum(...) - best_f
    family_score_f = best_f + 0.3*rest_f          # within-family correlation discount
  total_raw = sum(family_score_f over families)
  base = 100*(1 - exp(-0.9*total_raw))
  prior = storage.get_catalyst_prior(cat) or config.catalyst_spec(cat).drift_prior
  age_h = hours since cand.first_event_time (0 if unknown)
  earliness = clamp(1.2 - 0.1*age_h, 0.6, 1.2)
  magnitude = 1.0                                  # v1 proxy (documented)
  social_score = sum family_score_f for families in CONFIRM_ONLY_FAMILIES
  crowding_penalty = 15 * (social_score/total_raw if total_raw>0 else 0)
  staleness_penalty = 0 if age_h<=24 else min(20, (age_h-24)*0.5)
  score = clamp(base*prior*earliness*magnitude - crowding_penalty - staleness_penalty, 0, 100)

Then sets cand.raw_score, cand.confidence, cand.size_fraction (via
config.size_fraction_for) and cand.pillar_scores. Pillar veto adjustments are
applied by the judge (see brain/judge); this module records pillar reads when a
marketdata handle is available but never lets a pillar error change the score
here beyond the documented formula. Never raises; on error returns the candidate
with confidence 0 (fail safe: a candidate we cannot score cannot fire).
"""
from __future__ import annotations

import math

from ..config import (
    CONFIRM_ONLY_FAMILIES,
    catalyst_spec,
    get_config,
    size_fraction_for,
)
from ..models import Candidate
from ..util import age_seconds


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def score_candidate(cand: Candidate, cfg=None, storage=None, md=None) -> Candidate:
    """Score `cand` in place and return it. Fail safe to confidence 0 on error."""
    if cfg is None:
        cfg = get_config()
    try:
        # --- Per-family scores with the within-family correlation discount ---
        by_family: dict[str, list[float]] = {}
        for cs in cand.signals:
            raw = getattr(cs, "raw", None)
            if raw is None:
                continue
            fam = raw.family or ""
            rel = _safe_float(raw.base_reliability, 0.5)
            strength = _safe_float(raw.signal_strength, 0.5)
            by_family.setdefault(fam, []).append(rel * strength)

        family_scores: dict[str, float] = {}
        for fam, vals in by_family.items():
            if not vals:
                continue
            total = sum(vals)
            best = max(vals)
            rest = total - best
            family_scores[fam] = best + 0.3 * rest

        total_raw = sum(family_scores.values())

        # --- Base saturating curve ---
        base = 100.0 * (1.0 - math.exp(-0.9 * total_raw))

        # --- Catalyst prior (learned, else cold-start spec) ---
        prior = None
        if storage is not None:
            try:
                prior = storage.get_catalyst_prior(cand.catalyst)
            except Exception:
                prior = None
        if prior is None:
            prior = catalyst_spec(cand.catalyst).drift_prior
        prior = _safe_float(prior, 0.0)

        # --- Age / earliness / staleness ---
        age_h = _age_hours(cand.first_event_time)
        earliness = _clamp(1.2 - 0.1 * age_h, 0.6, 1.2)
        magnitude = 1.0  # v1: reference price at event not tracked; documented proxy.

        # --- Crowding penalty: how much conviction leans on confirm-only families ---
        social_score = sum(
            family_scores.get(fam, 0.0) for fam in CONFIRM_ONLY_FAMILIES
        )
        crowding_penalty = 15.0 * (social_score / total_raw if total_raw > 0 else 0.0)

        # --- Staleness penalty past the 24h freshness window ---
        staleness_penalty = 0.0 if age_h <= 24 else min(20.0, (age_h - 24.0) * 0.5)

        score = _clamp(
            base * prior * earliness * magnitude - crowding_penalty - staleness_penalty,
            0.0, 100.0,
        )

        cand.raw_score = float(total_raw)
        cand.confidence = float(score)
        cand.size_fraction = float(size_fraction_for(score))

        # Record the scoring breakdown for transparency / debugging.
        pillar = dict(cand.pillar_scores) if cand.pillar_scores else {}
        pillar["scoring"] = {
            "total_raw": round(total_raw, 4),
            "base": round(base, 2),
            "prior": round(prior, 4),
            "age_h": round(age_h, 2),
            "earliness": round(earliness, 3),
            "magnitude": magnitude,
            "social_score": round(social_score, 4),
            "crowding_penalty": round(crowding_penalty, 2),
            "staleness_penalty": round(staleness_penalty, 2),
            "family_scores": {k: round(v, 4) for k, v in family_scores.items()},
        }
        cand.pillar_scores = pillar
        return cand
    except Exception:
        # Fail safe: a candidate we cannot score must not look attractive.
        cand.raw_score = 0.0
        cand.confidence = 0.0
        cand.size_fraction = 0.0
        return cand


def _age_hours(first_event_time) -> float:
    """Hours since first_event_time; 0 if unknown (per the contract)."""
    if not first_event_time:
        return 0.0
    secs = age_seconds(first_event_time)
    if secs == float("inf") or secs < 0:
        return 0.0
    return secs / 3600.0


def _safe_float(v, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return f
