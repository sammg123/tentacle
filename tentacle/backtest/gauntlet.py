"""Rung -1 gauntlet: an honest proxy backtest that validates the drift edge and
the risk math before any capital is ever risked.

run_gauntlet returns a dict with two legs and a verdict:

    {
      "mastery_estimate": float 0..100,
      "earnings_drift":   {... PEAD study ...},
      "montecarlo":       {... ruin / expectancy simulation ...},
      "passed":           bool,
      "notes":            [str, ...]   # what is REAL vs PROXIED, stated plainly
    }

Honesty rules baked in here:
  - The earnings-drift leg is a REAL point-in-time study (yfinance earnings dates +
    forward closes). It is a PROXY for the live earnings_beat catalyst because the
    live system only fires on a confirmed BEAT with >=2 families, whereas here we
    measure drift after EVERY scheduled report (a strictly harder, more conservative
    bar). We say so in the notes.
  - The Monte Carlo leg is a SYNTHETIC simulation of the engine's own size bands and
    stop / target / ruin rules under a plausible per-trade edge. It proves the ruin
    floor holds and expectancy is positive given that edge. It does NOT prove the
    edge exists in the live tape; that is what leg 1 is for.
  - yfinance is imported INSIDE the function and guarded. If it is missing we skip
    leg 1, run only the Monte Carlo, and say so loudly in the notes (and we do NOT
    let the gauntlet "pass" on the Monte Carlo alone).

Nothing here places a trade, builds sources, or runs the engine. It only reads
prices and writes the earnings_beat catalyst prior to storage.
"""
from __future__ import annotations

import math
import random
from typing import Optional

from ..config import Config, catalyst_spec, get_config, size_fraction_for
from ..models import Catalyst
from ..util import get_logger

log = get_logger("tentacle.backtest.gauntlet")


# A default basket of large, liquid US names with frequent, clean earnings history.
# Used when the caller does not pass an explicit ticker list. Liquid names keep the
# yfinance pulls reliable and the drift measurement representative of what we trade.
DEFAULT_BASKET: list[str] = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AMD", "AVGO", "CRM",
    "ADBE", "NFLX", "INTC", "QCOM", "TXN", "ORCL", "CSCO", "PYPL", "MU", "NOW",
    "JPM", "BAC", "WFC", "V", "MA", "HD", "WMT", "COST", "PEP", "KO",
]

# The drift prior is a multiplier on the confidence score (see brain/confidence.py).
# The learning tuner keeps it inside [0.7, 1.3]; the gauntlet seeds within the same
# band so a cold start and a tuned state never disagree on the allowed range.
_PRIOR_LO = 0.7
_PRIOR_HI = 1.3

# Rung -1 promotion bar (config.rung_by_level(-1)): mastery >= 80 over >= 200 samples.
_PASS_MASTERY = 80.0
_PASS_SAMPLES = 200


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def run_gauntlet(cfg: Optional[Config] = None, tickers=None, years: int = 3) -> dict:
    """Run the Rung -1 gauntlet. Returns the verdict dict (see module docstring).

    cfg      : optional Config (defaults to get_config()).
    tickers  : optional basket for the earnings-drift study (defaults DEFAULT_BASKET).
    years    : lookback window for the earnings-drift study (default 3).
    """
    cfg = cfg or get_config()
    notes: list[str] = []

    basket = _clean_basket(tickers) or list(DEFAULT_BASKET)

    # ----------------------------------------------------- leg 1: earnings drift (PEAD)
    yf = _import_yfinance()
    if yf is None:
        earnings_drift = {
            "available": False,
            "samples": 0,
            "win_rate": None,
            "avg_drift": None,
            "profit_factor": None,
            "median_drift": None,
            "note": "yfinance not installed; earnings-drift leg SKIPPED (proxy unavailable).",
        }
        notes.append(
            "PROXY/REAL: yfinance is NOT installed, so the REAL earnings-drift study "
            "was skipped. Only the synthetic Monte Carlo ran. The gauntlet CANNOT "
            "pass on the Monte Carlo alone (no measured edge)."
        )
    else:
        earnings_drift = _earnings_drift_study(yf, basket, years, cfg)
        notes.append(
            "REAL: earnings-drift (PEAD) study over {n} ticker(s), {y}y lookback, "
            "measuring forward close-to-close drift over the earnings horizon "
            "({h:.0f}h). This is point-in-time price data, not a curve fit.".format(
                n=len(basket), y=years,
                h=catalyst_spec(Catalyst.EARNINGS_BEAT.value).horizon_hours,
            )
        )
        notes.append(
            "PROXY: live earnings_beat fires only on a CONFIRMED beat with >=2 "
            "independent families. This study measures drift after EVERY scheduled "
            "report (no beat filter, no confirmation), a strictly harder bar, so the "
            "live edge should be no worse than what is measured here."
        )
        _seed_earnings_prior(earnings_drift, cfg, notes)

    # ----------------------------------------------------- leg 2: risk / sizing Monte Carlo
    montecarlo = _risk_montecarlo(cfg)
    notes.append(
        "SYNTHETIC: Monte Carlo of {paths} equity paths through the confidence->size "
        "bands and the stop/target/ruin rules. ruin_deaths={d} (paths that hit the "
        "floor and stopped), floor_contained={c} (no path slid below the bounded worst "
        "case), expectancy_r={e}. Proves the ruin floor is CONTAINED and expectancy is "
        "positive given a plausible edge. It does NOT prove the edge exists live (leg 1 "
        "does that).".format(
            paths=montecarlo.get("paths", 0),
            d=montecarlo.get("ruin_deaths", 0),
            c=montecarlo.get("floor_contained"),
            e=montecarlo.get("expectancy_r"),
        )
    )

    # ----------------------------------------------------- verdict
    mastery_estimate = _mastery_estimate(earnings_drift, montecarlo)

    # Honest pass rule for Rung -1: a measured edge (leg 1 must be available with
    # enough samples) AND the risk math must hold (ruin never breached, positive
    # expectancy) AND the blended mastery estimate clears the rung bar.
    enough_samples = (
        earnings_drift.get("available")
        and int(earnings_drift.get("samples") or 0) >= _PASS_SAMPLES
    )
    risk_ok = (
        bool(montecarlo.get("floor_contained", False))
        and float(montecarlo.get("expectancy_r", 0.0) or 0.0) > 0.0
    )
    passed = bool(
        earnings_drift.get("available")
        and enough_samples
        and risk_ok
        and mastery_estimate >= _PASS_MASTERY
    )

    if not earnings_drift.get("available"):
        notes.append("VERDICT: cannot pass without the real earnings-drift leg.")
    elif not enough_samples:
        notes.append(
            "VERDICT: earnings-drift samples ({n}) below the Rung -1 minimum "
            "({need}); not enough evidence to pass yet.".format(
                n=int(earnings_drift.get("samples") or 0), need=_PASS_SAMPLES)
        )
    elif not risk_ok:
        notes.append(
            "VERDICT: risk math failed (floor_contained="
            f"{montecarlo.get('floor_contained')}, expectancy_r="
            f"{montecarlo.get('expectancy_r')}); blocking the pass."
        )
    elif not passed:
        notes.append(
            "VERDICT: mastery estimate {m:.1f} below the Rung -1 bar "
            "({bar:.0f}); not promoted.".format(m=mastery_estimate, bar=_PASS_MASTERY)
        )
    else:
        notes.append(
            "VERDICT: PASSED Rung -1 proxy (mastery {m:.1f} >= {bar:.0f}, "
            "{n} samples, ruin floor contained, positive expectancy). This seeds "
            "priors and clears the gate; it is NOT a forecast of returns.".format(
                m=mastery_estimate, bar=_PASS_MASTERY,
                n=int(earnings_drift.get("samples") or 0))
        )

    return {
        "mastery_estimate": round(mastery_estimate, 2),
        "earnings_drift": earnings_drift,
        "montecarlo": montecarlo,
        "passed": passed,
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# Leg 1: earnings drift (PEAD) study
# --------------------------------------------------------------------------- #
def _earnings_drift_study(yf, basket: list[str], years: int, cfg: Config) -> dict:
    """Measure forward drift over the earnings horizon for past earnings dates.

    For each ticker we pull the daily close history once, find past earnings dates,
    and for each one measure the close-to-close return from the first trading day
    AFTER the announcement out to the catalyst horizon (in trading days). This is a
    conservative entry (we do not assume we caught the open gap) and the same hold
    horizon the live system uses for earnings_beat.

    Returns a dict with samples / win_rate / avg_drift / median_drift / profit_factor
    and per-leg detail. Every yfinance call is guarded; any failure on a ticker just
    drops that ticker from the sample (never raises).
    """
    horizon_hours = catalyst_spec(Catalyst.EARNINGS_BEAT.value).horizon_hours
    # Convert the hold horizon to trading days (~6.5h sessions, but the prior is in
    # calendar hours of holding; PEAD is a multi-day effect). Use calendar->trading
    # day conversion of 24h per calendar day, then 5/7 trading days, min 1 day.
    calendar_days = max(1.0, horizon_hours / 24.0)
    horizon_td = max(1, int(round(calendar_days * 5.0 / 7.0)))

    flat_band = 0.01  # +-1% counts as flat, matching the outcomes labeler
    drifts: list[float] = []
    per_ticker: dict[str, int] = {}
    # Lightweight diagnostics so a 0-sample result is actionable (which yf call was
    # empty). get_earnings_dates needs lxml; if lxml is absent every earnings-date
    # fetch comes back empty and the leg cannot run, so we surface that distinctly.
    n_history_ok = 0
    n_earnings_ok = 0

    pd = _import_pandas()

    for tk in basket:
        try:
            hist = _ticker_close_history(yf, tk, years)
            if hist is None or len(hist) < (horizon_td + 2):
                continue
            n_history_ok += 1
            edates = _earnings_dates(yf, tk)
            if not edates:
                continue
            n_earnings_ok += 1
            closes = hist  # a list of (date, close) sorted ascending
            n_added = 0
            for ed in edates:
                d = _post_event_drift(closes, ed, horizon_td)
                if d is None:
                    continue
                drifts.append(d)
                n_added += 1
            if n_added:
                per_ticker[tk] = n_added
        except Exception as exc:  # noqa: BLE001  swallow per-ticker failures
            log.debug("earnings drift failed for %s: %s", tk, exc)
            continue

    # use pandas only if it helped the median; otherwise pure-python below
    _ = pd

    samples = len(drifts)
    if samples == 0:
        if n_history_ok == 0:
            reason = ("price history was empty for the whole basket (rate limit or "
                      "network). No prior seeded.")
        elif n_earnings_ok == 0:
            reason = ("price history loaded but get_earnings_dates returned nothing "
                      "for the whole basket. This usually means the optional 'lxml' "
                      "dependency is missing (yfinance scrapes the earnings table with "
                      "it) or the feed is rate limited. No prior seeded.")
        else:
            reason = ("earnings dates loaded but no usable forward window (history too "
                      "short for the horizon). No prior seeded.")
        return {
            "available": True,
            "samples": 0,
            "win_rate": None,
            "avg_drift": None,
            "median_drift": None,
            "profit_factor": None,
            "horizon_trading_days": horizon_td,
            "tickers_used": 0,
            "tickers_with_history": n_history_ok,
            "tickers_with_earnings": n_earnings_ok,
            "note": "Earnings-drift leg produced 0 samples: " + reason,
        }

    wins = sum(1 for d in drifts if d > flat_band)
    losses = sum(1 for d in drifts if d < -flat_band)
    win_rate = wins / samples
    avg_drift = sum(drifts) / samples
    median_drift = _median(drifts)
    gross_win = sum(d for d in drifts if d > 0)
    gross_loss = -sum(d for d in drifts if d < 0)
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    return {
        "available": True,
        "samples": samples,
        "win_rate": round(win_rate, 4),
        "avg_drift": round(avg_drift, 5),
        "median_drift": round(median_drift, 5),
        "profit_factor": (round(profit_factor, 4)
                          if profit_factor != float("inf") else None),
        "wins": wins,
        "losses": losses,
        "flats": samples - wins - losses,
        "horizon_trading_days": horizon_td,
        "tickers_used": len(per_ticker),
        "per_ticker_samples": per_ticker,
        "note": ("Forward close-to-close drift after EVERY scheduled report (no beat "
                 "filter) over the earnings horizon. Conservative proxy for live PEAD."),
    }


def _ticker_close_history(yf, ticker: str, years: int):
    """Return a list of (date, close) tuples sorted ascending, or None. Guarded."""
    try:
        period = f"{max(1, int(years))}y"
        t = yf.Ticker(ticker)
        hist = t.history(period=period, auto_adjust=True)
        if hist is None or not len(hist):
            return None
        out: list[tuple] = []
        for idx, close in zip(hist.index, hist["Close"]):
            try:
                d = idx.date() if hasattr(idx, "date") else idx
                c = float(close)
                if c > 0:
                    out.append((d, c))
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda x: x[0])
        return out or None
    except Exception as exc:  # noqa: BLE001
        log.debug("close history failed for %s: %s", ticker, exc)
        return None


def _earnings_dates(yf, ticker: str):
    """Return a list of PAST earnings dates (datetime.date), or [] on any failure."""
    from datetime import datetime, timezone
    try:
        t = yf.Ticker(ticker)
        # limit is generous; yfinance returns a DataFrame indexed by Timestamp.
        ed = t.get_earnings_dates(limit=40)
        if ed is None or not len(ed):
            return []
        today = datetime.now(timezone.utc).date()
        out = []
        for idx in ed.index:
            try:
                d = idx.date() if hasattr(idx, "date") else idx
                if d <= today:
                    out.append(d)
            except Exception:  # noqa: BLE001
                continue
        # de-dup and sort
        return sorted(set(out))
    except Exception as exc:  # noqa: BLE001
        log.debug("earnings dates failed for %s: %s", ticker, exc)
        return []


def _post_event_drift(closes, event_date, horizon_td: int):
    """Drift from the first trading close AFTER event_date out to horizon_td days.

    closes: list of (date, close) ascending. We enter at the first session strictly
    after the announcement date (so we never use the announcement-day close, which
    can straddle the report) and exit horizon_td trading sessions later. Returns the
    fractional return, or None if there is not enough forward data.
    """
    try:
        # find the first index with date strictly after the event date
        entry_i = None
        for i, (d, _c) in enumerate(closes):
            if d > event_date:
                entry_i = i
                break
        if entry_i is None:
            return None
        exit_i = entry_i + horizon_td
        if exit_i >= len(closes):
            return None
        entry_px = closes[entry_i][1]
        exit_px = closes[exit_i][1]
        if entry_px <= 0:
            return None
        return (exit_px - entry_px) / entry_px
    except Exception:  # noqa: BLE001
        return None


def _seed_earnings_prior(earnings_drift: dict, cfg: Config, notes: list[str]) -> None:
    """Write the earnings_beat drift_prior to storage from the study (guarded).

    Map measured average drift to a prior in [0.7, 1.3], centered on 1.0 at zero
    drift. We only seed when there is a meaningful sample so a thin / rate-limited
    pull never moves the prior. Storage failure is swallowed.
    """
    samples = int(earnings_drift.get("samples") or 0)
    avg_drift = earnings_drift.get("avg_drift")
    if samples < 30 or avg_drift is None:
        notes.append(
            "PRIOR: earnings_beat drift_prior NOT seeded (only {n} sample(s); need "
            ">=30 to move the prior).".format(n=samples)
        )
        return
    try:
        # Scale: +5% avg drift -> +0.3 prior (toward the cap). Conservative slope so
        # one strong window cannot over-trust the catalyst. Clamp into the band.
        prior = 1.0 + float(avg_drift) * 6.0
        prior = max(_PRIOR_LO, min(_PRIOR_HI, prior))
        wins = int(earnings_drift.get("wins") or 0)
        # absolute upsert (replace the cold-start), recording the sample evidence.
        from ..storage import get_storage
        storage = get_storage()
        # set drift_prior directly: pass the new absolute value and 0 deltas for the
        # cumulative counters so we do not double-count; then record evidence counts.
        storage.upsert_catalyst_prior(
            Catalyst.EARNINGS_BEAT.value,
            drift_prior=prior,
            samples=samples,
            wins=wins,
            sum_drift=float(avg_drift) * samples,
        )
        earnings_drift["seeded_prior"] = round(prior, 4)
        notes.append(
            "PRIOR: seeded earnings_beat drift_prior = {p:.3f} from avg drift "
            "{a:+.2%} over {n} samples (clamped to [{lo},{hi}]).".format(
                p=prior, a=float(avg_drift), n=samples, lo=_PRIOR_LO, hi=_PRIOR_HI)
        )
    except Exception as exc:  # noqa: BLE001
        notes.append(f"PRIOR: failed to seed earnings_beat prior ({exc}); ignored.")


# --------------------------------------------------------------------------- #
# Leg 2: risk / sizing Monte Carlo
# --------------------------------------------------------------------------- #
def _risk_montecarlo(cfg: Config, paths: int = 2000, trades_per_path: int = 200) -> dict:
    """Simulate equity paths through the engine's size bands and stop/target/ruin rules.

    Each simulated trade:
      - draws a confidence score from a plausible distribution centered above the
        55 fire floor (so most trades are real-size, the worst case for ruin),
      - sizes it with config.size_fraction_for capped by max_position_fraction and
        the fractional-Kelly ceiling (the same caps risk/sizing uses),
      - resolves win/loss with a plausible edge: a winner moves to +target_pct, a
        loser to -stop_pct, with a base win probability that gives a small positive
        per-trade expectancy. We deliberately use a MODEST edge so a pass is not a
        product of an optimistic assumption.
      - applies the daily-loss halt and the ruin floor exactly as risk/caps defines
        them (stage_start * (1 - ruin_floor_pct)).

    WHAT "the engine cannot breach the ruin floor" MEANS HERE, honestly: crossing
    the floor is the DEATH event. The risk law (risk/caps.ruin_breached) fires the
    instant equity touches the floor and the engine STOPS trading (ladder demotes a
    rung and forces re-qualify). So a path can dip to or just past the floor on the
    fill that crosses it, but it can never grind FAR below the floor, because no new
    trade is opened after death. The real safety claim is therefore bounded overshoot:
    the worst equity must never fall below floor minus ONE max position loss. We
    measure both: ruin_deaths (paths that hit the floor; expected to be rare with a
    real edge) and floor_contained (no path ever overshoots past the bounded worst
    case). floor_contained is the hard pass condition the verdict uses.

    Returns the death count, the containment flag, expectancy_r, median / worst
    final equity, and the assumed edge so the result is reproducible and honest.
    """
    risk = cfg.risk
    stop_pct = float(getattr(risk, "per_trade_stop_pct", 0.08) or 0.08)
    target_pct = float(getattr(risk, "per_trade_target_pct", 0.15) or 0.15)
    ruin_floor_pct = float(getattr(risk, "ruin_floor_pct", 0.40) or 0.40)
    daily_loss_halt_pct = float(getattr(risk, "daily_loss_halt_pct", 0.10) or 0.10)
    max_pos_frac = float(getattr(risk, "max_position_fraction", 1.0) or 1.0)
    kelly_cap = float(getattr(risk, "kelly_fraction_cap", 0.50) or 0.50)
    max_open = max(1, int(getattr(risk, "max_open_positions", 3) or 3))

    # Plausible base edge: win probability for a real-size trade. Chosen so the
    # per-trade expectancy in R is small but positive (honest, not heroic).
    base_win_prob = 0.45  # below 0.5; the asymmetric target/stop carries the edge

    rng = random.Random(1729)  # fixed seed: the math verdict is reproducible
    stage_start = 100.0  # normalized stage capital; results are scale-free fractions
    ruin_level = stage_start * (1.0 - ruin_floor_pct)

    # Effective per-position cap (fraction of rung capital actually deployable).
    pos_cap = min(max_pos_frac, kelly_cap)
    # The most a single position can lose is its stake (cap * equity) times the stop.
    # On the fill that crosses the floor, equity is still above the floor, so it can
    # overshoot by at most this fraction of the pre-fill equity. The hard safety bound
    # is floor minus one max position loss measured at the floor level.
    max_single_loss_at_floor = ruin_level * pos_cap * stop_pct
    safe_floor_bound = ruin_level - max_single_loss_at_floor

    final_equities: list[float] = []
    ruin_deaths = 0
    min_equity_ever = stage_start
    overshoot_breaches = 0  # paths that fell BELOW the bounded worst case (should be 0)
    all_r: list[float] = []

    for _ in range(paths):
        equity = stage_start
        died = False
        # group trades into "days" so the daily-loss halt is meaningful.
        i = 0
        while i < trades_per_path and not died:
            # one trading "day": up to max_open positions, then check the halt.
            day_start_equity = equity
            day_realized = 0.0
            for _slot in range(max_open):
                if i >= trades_per_path:
                    break
                i += 1
                score = _draw_score(rng)
                frac = size_fraction_for(score)
                if frac <= 0:
                    continue  # below the fire floor: no trade, the gate blocked it
                frac = min(frac, pos_cap)
                stake = equity * frac
                # resolve the trade with the modest edge
                win = rng.random() < base_win_prob
                move = target_pct if win else -stop_pct
                pnl = stake * move
                equity += pnl
                day_realized += pnl
                if equity < min_equity_ever:
                    min_equity_ever = equity
                if equity < safe_floor_bound:
                    overshoot_breaches += 1
                # per-trade R (P&L expressed in units of the risked stop amount)
                risk_amt = stake * stop_pct
                if risk_amt > 0:
                    all_r.append(pnl / risk_amt)
                # ruin check after EVERY fill: the floor is the DEATH trigger. Once
                # touched, the engine stops trading (no new positions opened).
                if equity <= ruin_level:
                    ruin_deaths += 1
                    died = True
                    break
                # daily-loss halt: stop opening for the day once breached
                if day_realized <= -daily_loss_halt_pct * day_start_equity:
                    break
        final_equities.append(equity)

    final_equities.sort()
    median_final = _median(final_equities)
    worst_final = final_equities[0] if final_equities else stage_start
    best_final = final_equities[-1] if final_equities else stage_start
    expectancy_r = (sum(all_r) / len(all_r)) if all_r else 0.0
    # The hard safety claim: NO path ever fell below the bounded worst case (floor
    # minus one max position loss). Death at the floor is allowed and expected; an
    # UNCONTAINED slide far below it is not.
    floor_contained = (overshoot_breaches == 0)

    return {
        "paths": paths,
        "trades_per_path": trades_per_path,
        "ruin_floor_level": round(ruin_level, 4),
        "stage_start": stage_start,
        "ruin_deaths": ruin_deaths,
        "ruin_death_rate": round(ruin_deaths / paths, 6) if paths else 0.0,
        # kept for backward compatibility: a "breach" of CONTAINMENT (not a death).
        "ruin_breaches": overshoot_breaches,
        "safe_floor_bound": round(safe_floor_bound, 4),
        "min_equity_ever": round(min_equity_ever, 4),
        "floor_contained": floor_contained,
        "expectancy_r": round(expectancy_r, 5),
        "median_final_equity": round(median_final, 4),
        "worst_final_equity": round(worst_final, 4),
        "best_final_equity": round(best_final, 4),
        "median_return_pct": round((median_final / stage_start - 1.0), 5),
        "assumed_base_win_prob": base_win_prob,
        "assumed_target_pct": target_pct,
        "assumed_stop_pct": stop_pct,
        "note": ("Synthetic. Confidence drawn above the 55 fire floor (worst case for "
                 "ruin), sized by the real bands + Kelly cap, resolved at +target / "
                 "-stop with a MODEST 45% win prob (edge from asymmetry). 'ruin_deaths' "
                 "= paths that hit the floor (death fires and trading stops); "
                 "'floor_contained' = no path ever slid below floor minus one max "
                 "position loss. Containment, not zero deaths, is the safety claim. "
                 "Not a return forecast."),
    }


def _draw_score(rng: random.Random) -> float:
    """Draw a plausible confidence score (0..100).

    Mixture: most candidates that reach scoring are mediocre (below the 55 floor and
    thus blocked), a meaningful minority clear the bands. Centering mass near and
    above the floor is the WORST case for ruin (more real-size trades), which is what
    we want to stress.
    """
    # 35% are sub-floor noise (blocked), 65% are tradeable, skewed toward the lower
    # size bands so we are not assuming a stream of max-size convictions.
    if rng.random() < 0.35:
        return rng.uniform(0.0, 55.0)
    # tradeable: triangular-ish toward the 55..80 range, occasional high-conviction.
    base = rng.triangular(55.0, 100.0, 68.0)
    return max(0.0, min(100.0, base))


# --------------------------------------------------------------------------- #
# Mastery estimate from the two legs
# --------------------------------------------------------------------------- #
def _mastery_estimate(earnings_drift: dict, montecarlo: dict) -> float:
    """Blend the two legs into a 0..100 mastery estimate.

    This mirrors the spirit of ladder.mastery (profit factor, win rate, expectancy,
    sample size, plus a hard risk gate) but is computed from the gauntlet's own
    measured / simulated statistics since there are no logged outcomes yet. If the
    earnings-drift leg is unavailable the estimate is capped low: we will not let a
    synthetic-only run look masterful.
    """
    if not earnings_drift.get("available"):
        # No measured edge: mastery is meaningless. Return a low number so the
        # gauntlet cannot pass on the Monte Carlo alone.
        return 0.0

    samples = int(earnings_drift.get("samples") or 0)
    if samples == 0:
        return 0.0

    win_rate = float(earnings_drift.get("win_rate") or 0.0)
    avg_drift = float(earnings_drift.get("avg_drift") or 0.0)
    pf = earnings_drift.get("profit_factor")
    pf = float(pf) if pf is not None else 4.0  # None means inf (all winners); cap high

    # Component 1: profit factor squashed x/(x+1).
    c_pf = pf / (pf + 1.0)
    # Component 2: win rate as measured.
    c_wr = max(0.0, min(1.0, win_rate))
    # Component 3: expectancy. Express avg drift in R (units of the stop) via the
    # Monte Carlo's assumed stop, logistic-mapped (0R -> 0.5).
    stop = float(montecarlo.get("assumed_stop_pct") or 0.08) or 0.08
    avg_r = avg_drift / stop
    c_exp = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, avg_r))))
    # Component 4: sample size saturation n/(n+threshold) with the rung-200 threshold
    # so a 200-sample study sits at 0.5 and only a large record approaches 1.
    c_n = samples / (samples + _PASS_SAMPLES)
    # Component 5: risk integrity (hard gate). Floor contained (no uncontrolled slide
    # below the bounded worst case) + positive expectancy in the Monte Carlo -> full
    # credit; otherwise zero.
    contained = bool(montecarlo.get("floor_contained", False))
    mc_exp = float(montecarlo.get("expectancy_r", 0.0) or 0.0)
    c_risk = 1.0 if (contained and mc_exp > 0.0) else 0.0

    # Weighted blend (weights chosen to echo MasteryWeights emphasis: profit factor,
    # calibration-like risk integrity, and expectancy carry the most).
    score01 = (
        0.25 * c_pf
        + 0.15 * c_wr
        + 0.20 * c_exp
        + 0.15 * c_n
        + 0.25 * c_risk
    )
    mastery = max(0.0, min(100.0, score01 * 100.0))

    # Hard gate: a ruin breach in the math leg caps mastery far below the bar no
    # matter how good the drift looked.
    if c_risk == 0.0:
        mastery = min(mastery, 40.0)

    return mastery


# --------------------------------------------------------------------------- #
# Small local helpers
# --------------------------------------------------------------------------- #
def _clean_basket(tickers) -> list[str]:
    """Normalize a caller-supplied ticker list to upper-case, de-duped, <= 60."""
    if not tickers:
        return []
    out: list[str] = []
    seen: set[str] = set()
    try:
        for t in tickers:
            if not t:
                continue
            s = str(t).strip().upper()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    except Exception:  # noqa: BLE001
        return []
    return out[:60]


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _import_yfinance():
    """Import yfinance lazily, guarded. Returns the module or None."""
    try:
        import yfinance as yf  # type: ignore
        return yf
    except Exception as exc:  # noqa: BLE001
        log.debug("yfinance unavailable: %s", exc)
        return None


def _import_pandas():
    """Import pandas lazily, guarded. Returns the module or None."""
    try:
        import pandas as pd  # type: ignore
        return pd
    except Exception:  # noqa: BLE001
        return None
