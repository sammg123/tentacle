"""Confirmation: group classified signals into Candidates and apply the gate.

A Candidate is one (ticker, catalyst) hypothesis supported by a bundle of
classified signals. We build a candidate for EVERY group (even single-signal
ones) so the shadow-logging path can learn from the road not taken. The decision
to fire is NOT made here; that is the risk module's law. Here we only build the
candidates and report whether the confirmation gate passes, with reasons.

The gate (config.GateConfig):
  * >= min_sources independent source_ids, AND
  * >= min_families distinct families, AND
  * at least one of those families is in config.TRIGGER_FAMILIES, AND
  * the signals fall within confirm_window_min minutes of each other.

Confirm-only families (social, flow, ...) can corroborate but never trigger
alone: that is the weighting law (two wires echoing one press release, or a
social pump, must not look like independent confirmation).
"""
from __future__ import annotations

from typing import Optional

from ..config import TRIGGER_FAMILIES, get_config
from ..models import Candidate, ClassifiedSignal
from ..util import parse_dt


def build_candidates(classified: list[ClassifiedSignal], cfg=None) -> list[Candidate]:
    """Group classified signals by (ticker, catalyst) into Candidates.

    Returns ALL groups, single-signal ones included. first/last_event_time are
    the min/max of the group's signal event_times (falling back to fetched_at
    when an event_time is missing). Never raises; returns [] on error.
    """
    if cfg is None:
        cfg = get_config()
    try:
        groups: dict[tuple[str, str], list[ClassifiedSignal]] = {}
        for cs in classified or []:
            if cs is None:
                continue
            ticker = (cs.ticker or "").upper().strip()
            catalyst = cs.catalyst or ""
            if not ticker or not catalyst:
                continue
            groups.setdefault((ticker, catalyst), []).append(cs)

        out: list[Candidate] = []
        for (ticker, catalyst), sigs in groups.items():
            first_iso, last_iso = _event_span(sigs)
            out.append(Candidate(
                ticker=ticker,
                catalyst=catalyst,
                signals=list(sigs),
                first_event_time=first_iso,
                last_event_time=last_iso,
            ))
        return out
    except Exception:
        return []


def _event_span(sigs: list[ClassifiedSignal]) -> tuple[Optional[str], Optional[str]]:
    """Return (first_iso, last_iso) using event_time, falling back to fetched_at."""
    times: list[tuple] = []  # (datetime, iso_str)
    for cs in sigs:
        raw = getattr(cs, "raw", None)
        if raw is None:
            continue
        iso = raw.event_time or raw.fetched_at
        dt = parse_dt(iso)
        if dt is not None and iso:
            times.append((dt, iso))
    if not times:
        return None, None
    first = min(times, key=lambda x: x[0])
    last = max(times, key=lambda x: x[0])
    return first[1], last[1]


def passes_confirmation_gate(cand: Candidate, cfg=None) -> tuple[bool, list[str]]:
    """Return (passes, reasons). Reasons are returned either way (shadow logging).

    FAIL SAFE: any error -> (False, [...]). Unknown event times -> the window
    check fails closed (we cannot prove the signals are contemporaneous).
    """
    if cfg is None:
        cfg = get_config()
    reasons: list[str] = []
    try:
        gate = cfg.gate
        source_ids = cand.source_ids
        families = cand.families

        n_sources = len(source_ids)
        n_families = len(families)
        trigger_overlap = families & set(TRIGGER_FAMILIES)
        has_trigger = bool(trigger_overlap)

        ok_sources = n_sources >= gate.min_sources
        ok_families = n_families >= gate.min_families

        # Window check: all signal event times within confirm_window_min minutes.
        within_window, span_min = _within_window(cand, gate.confirm_window_min)

        reasons.append(
            f"sources={n_sources}/{gate.min_sources} "
            f"({'ok' if ok_sources else 'too few'})"
        )
        reasons.append(
            f"families={n_families}/{gate.min_families} "
            f"({'ok' if ok_families else 'too few'})"
        )
        if has_trigger:
            reasons.append(f"trigger family present: {sorted(trigger_overlap)}")
        else:
            reasons.append(
                "no trigger family (confirm-only families cannot fire alone): "
                f"{sorted(families)}"
            )
        if span_min is None:
            reasons.append("event window unknown -> fail safe")
        else:
            reasons.append(
                f"window span={span_min:.0f}min/{gate.confirm_window_min}min "
                f"({'ok' if within_window else 'too wide'})"
            )

        passed = ok_sources and ok_families and has_trigger and within_window
        return passed, reasons
    except Exception:
        reasons.append("confirmation gate error -> fail safe (blocked)")
        return False, reasons


def _within_window(cand: Candidate, window_min: int) -> tuple[bool, Optional[float]]:
    """Return (within, span_minutes). span None == unknown (fail safe -> not within)."""
    dts = []
    for cs in cand.signals:
        raw = getattr(cs, "raw", None)
        if raw is None:
            continue
        dt = parse_dt(raw.event_time or raw.fetched_at)
        if dt is not None:
            dts.append(dt)
    if len(dts) < 1:
        return False, None
    if len(dts) == 1:
        # A single timestamp trivially spans 0 minutes. (Whether one source is
        # enough is the min_sources check, not the window check.)
        return True, 0.0
    span_sec = (max(dts) - min(dts)).total_seconds()
    span_min = span_sec / 60.0
    return span_min <= float(window_min), span_min
