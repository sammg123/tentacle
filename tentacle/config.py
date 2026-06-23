"""Central configuration: every threshold, weight, cap, and ladder rung.

This file encodes the numbers the design prompt fixes. Anything tunable at run
time is read from the environment with a safe default, so the only thing strictly
required to run is CONTACT_EMAIL (and it already defaults to the owner's email).

The learning loop does NOT edit this file; it tunes per-source reliability and
per-catalyst priors in the database (see storage + learning). These are the
*defaults* / cold-start values and the *hard limits* the model can never cross.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .models import Catalyst


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on", "y"}


def _s(name: str, default: str) -> str:
    return os.environ.get(name, default)


# --------------------------------------------------------------------------- #
# The three "fill these in" inputs (only the email is strictly required)
# --------------------------------------------------------------------------- #
CONTACT_EMAIL = _s("CONTACT_EMAIL", "goldfedergpt@gmail.com")
BROKER = _s("BROKER", "alpaca")              # alpaca | simulator | robinhood
STARTING_CAPITAL = _f("STARTING_CAPITAL", 1000.0)  # the TOP rung target


# --------------------------------------------------------------------------- #
# The graduation ladder. Capital is EARNED rung by rung. Full size is the TOP,
# never the default. The bot starts at rung -1 ($0, backtest).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rung:
    level: int
    name: str
    capital: float
    promote_mastery: float          # Mastery score needed to advance
    promote_samples: int            # how many qualifying samples/trades
    note: str = ""


def build_ladder() -> list[Rung]:
    return [
        Rung(-1, "Backtest Gauntlet", 0.0, 80.0, 200,
             "profitable across years, OOS, multi-regime"),
        Rung(0, "Live Shadow/Paper", 0.0, 85.0, 40,
             "forward, no-hindsight signals"),
        Rung(1, "Micro", 25.0, 85.0, 20, ">=20 green trades, sustained"),
        Rung(2, "Small", 75.0, 87.0, 20, ""),
        Rung(3, "Half", 250.0, 90.0, 20, ""),
        Rung(4, "Full", STARTING_CAPITAL, 92.0, 30,
             "sustained over the longest window"),
    ]


LADDER: list[Rung] = build_ladder()


def rung_by_level(level: int) -> Rung:
    for r in LADDER:
        if r.level == level:
            return r
    return LADDER[0]


# --------------------------------------------------------------------------- #
# The FIRE gate (section 4). ALL must be true to place a trade.
# --------------------------------------------------------------------------- #
@dataclass
class GateConfig:
    min_sources: int = _i("MIN_SOURCES", 2)            # >=2 independent sources
    min_families: int = _i("MIN_FAMILIES", 2)          # from >=2 signal families
    confirm_window_min: int = _i("CONFIRM_WINDOW_MIN", 240)  # tight window (min)
    earliness_max_runup: float = _f("EARLINESS_MAX_RUNUP", 0.04)  # +4% since src
    liquidity_min_dollar_vol: float = _f("LIQ_MIN_DOLLAR_VOL", 2_000_000.0)
    liquidity_min_price: float = _f("LIQ_MIN_PRICE", 1.50)
    no_chase_vwap_band: float = _f("NO_CHASE_VWAP_BAND", 0.03)  # don't pay >3% VWAP
    require_market_open: bool = _b("REQUIRE_MARKET_OPEN", True)
    require_uptrend_regime: bool = _b("REQUIRE_UPTREND_REGIME", True)


# --------------------------------------------------------------------------- #
# Confidence -> size bands (section 5). Fraction of the CURRENT RUNG's capital.
# --------------------------------------------------------------------------- #
SIZE_BANDS: list[tuple[float, float]] = [
    (95.0, 1.00),
    (85.0, 0.70),
    (70.0, 0.45),
    (55.0, 0.20),
    (0.0, 0.0),     # below 55 -> no trade
]


def size_fraction_for(score: float) -> float:
    for threshold, frac in SIZE_BANDS:
        if score >= threshold:
            return frac
    return 0.0


# --------------------------------------------------------------------------- #
# Catalyst priors + hold horizons. These are COLD-START values; the learning
# loop overrides per-catalyst drift priors in the DB over time.
#   drift_prior:  multiplier on the score (how reliably this category drifts UP)
#   horizon_hours: how long we hold to capture the drift
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CatalystSpec:
    drift_prior: float
    horizon_hours: float
    can_trigger: bool           # may this category trigger a trade at all?


CATALYST_SPECS: dict[str, CatalystSpec] = {
    Catalyst.INDEX_INCLUSION.value: CatalystSpec(1.15, 72.0, True),   # most mechanical
    Catalyst.EARNINGS_BEAT.value:   CatalystSpec(1.05, 96.0, True),   # PEAD, days
    Catalyst.FDA_APPROVAL.value:    CatalystSpec(1.00, 24.0, True),   # binary, fast
    Catalyst.MNA_TARGET.value:      CatalystSpec(1.10, 48.0, True),
    Catalyst.GOV_CONTRACT.value:    CatalystSpec(0.95, 72.0, True),
    Catalyst.ACTIVIST_STAKE.value:  CatalystSpec(1.00, 120.0, True),
    Catalyst.INSIDER_CLUSTER.value: CatalystSpec(0.90, 120.0, True),
    Catalyst.AI_QUANTUM_BREAKOUT.value: CatalystSpec(0.85, 48.0, True),
    Catalyst.UNKNOWN.value:         CatalystSpec(0.0, 24.0, False),   # never fires
}


def catalyst_spec(catalyst: str) -> CatalystSpec:
    return CATALYST_SPECS.get(catalyst, CATALYST_SPECS[Catalyst.UNKNOWN.value])


# --------------------------------------------------------------------------- #
# Source reliability cold-start weights by TIER. Tuned per-source by learning.
# Tier1-2 / primary may TRIGGER. Social/flow/alt are CONFIRMATION ONLY.
# --------------------------------------------------------------------------- #
TIER_BASE_WEIGHT: dict[int, float] = {
    1: 1.00,   # primary: SEC, newswire, FDA, index, halts
    2: 0.75,   # news APIs
    3: 0.35,   # social (confirm only)
    4: 0.45,   # flow (confirm only)
    5: 0.30,   # expanded universe (lead/confirm only)
}

# Families that are allowed to be the SOLE trigger family for a real trade.
# Everything else is confirmation-only and can never trigger alone (pump risk).
TRIGGER_FAMILIES: set[str] = {
    "sec_filing", "newswire", "regulatory", "index", "halt", "earnings", "news",
}
CONFIRM_ONLY_FAMILIES: set[str] = {
    "social", "flow", "crowd_forecast", "dev_ecosystem", "demand", "gov", "alt",
}


# --------------------------------------------------------------------------- #
# Risk / survival ethos (section 6). HARD limits enforced OUTSIDE the model.
# --------------------------------------------------------------------------- #
@dataclass
class RiskConfig:
    loss_aversion_lambda: float = _f("LOSS_AVERSION_LAMBDA", 2.5)
    ruin_floor_pct: float = _f("RUIN_FLOOR_PCT", 0.40)        # -40% of stage start = DEATH
    per_trade_stop_pct: float = _f("PER_TRADE_STOP_PCT", 0.08)  # hard stop per position
    per_trade_target_pct: float = _f("PER_TRADE_TARGET_PCT", 0.15)
    daily_loss_halt_pct: float = _f("DAILY_LOSS_HALT_PCT", 0.10)  # halt for the day
    max_open_positions: int = _i("MAX_OPEN_POSITIONS", 3)
    max_position_fraction: float = _f("MAX_POSITION_FRACTION", 1.00)  # of rung cap
    kelly_fraction_cap: float = _f("KELLY_FRACTION_CAP", 0.50)  # fractional Kelly ceiling
    settle_days: int = _i("SETTLE_DAYS", 1)                  # T+1 for cash accounts
    cash_account: bool = _b("CASH_ACCOUNT", True)            # GFV tracking on


# --------------------------------------------------------------------------- #
# Regime filter (SPY trend) + market hours
# --------------------------------------------------------------------------- #
@dataclass
class RegimeConfig:
    benchmark: str = _s("REGIME_BENCHMARK", "SPY")
    fast_ma: int = _i("REGIME_FAST_MA", 20)
    slow_ma: int = _i("REGIME_SLOW_MA", 50)
    # tape is "down" when fast < slow; trades blocked unless override


# --------------------------------------------------------------------------- #
# Engine loop cadence
# --------------------------------------------------------------------------- #
@dataclass
class EngineConfig:
    cycle_seconds_market_open: int = _i("CYCLE_SECONDS_OPEN", 90)
    cycle_seconds_market_closed: int = _i("CYCLE_SECONDS_CLOSED", 900)
    max_candidates_to_judge: int = _i("MAX_JUDGE_CANDIDATES", 3)
    armed: bool = _b("TENTACLE_ARMED", False)   # operator must explicitly ARM
    # When True, the capital ladder is OFF: the bot is pinned to the full rung (STARTING_CAPITAL,
    # e.g. $100k) and there is no earn-your-allowance, no promote/demote, and no ruin-death halt.
    # It just paper-trades the full account until told otherwise.
    no_ladder: bool = _b("TENTACLE_NO_LADDER", False)
    db_path: str = _s("TENTACLE_DB", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tentacle.db"))


# --------------------------------------------------------------------------- #
# Mastery score weights (section 7). Blend that decides promotion.
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# The Mother: meta-learning coach. Thresholds for her two eyes and for promotion.
# She edits only the kid's reasoning policy, never the risk law (see MOTHER.md).
# --------------------------------------------------------------------------- #
@dataclass
class MotherConfig:
    # Eye 2 (financial guardian) alert thresholds (aggregate, noise-gated)
    drawdown_alert_pct: float = _f("MOTHER_DRAWDOWN_ALERT", 0.20)   # early brake, well above the -40% ruin death
    loss_streak_alert: int = _i("MOTHER_LOSS_STREAK", 4)
    min_edge_samples: int = _i("MOTHER_MIN_EDGE_SAMPLES", 30)
    calibration_alert: float = _f("MOTHER_CALIB_ALERT", 0.30)
    rolling_window: int = _i("MOTHER_ROLLING_WINDOW", 20)
    auto_defensive: bool = _b("MOTHER_AUTO_DEFENSIVE", False)       # disarm on a drawdown alert
    guardian_in_loop: bool = _b("MOTHER_GUARDIAN_IN_LOOP", True)    # cheap, run every cycle
    # Eye 1 (blind process review)
    review_batch: int = _i("MOTHER_REVIEW_BATCH", 20)
    # "performing well" bars (anti-overcorrection: do not fix a winner)
    well_process_score: float = _f("MOTHER_WELL_PROCESS", 80.0)
    well_calibration: float = _f("MOTHER_WELL_CALIB", 0.15)
    # heavy-pass gate: do not run the blind review/propose until enough NEW decisions
    # have accrued, so the mother never grades noise.
    min_new_decisions: int = _i("MOTHER_MIN_NEW_DECISIONS", 15)
    # champion/challenger promotion
    promotion_cooldown_hours: float = _f("MOTHER_PROMOTION_COOLDOWN_H", 168.0)  # ~weekly
    promotion_margin: float = _f("MOTHER_PROMOTION_MARGIN", 10.0)   # process-score points (rubric fallback)
    # forward shadow A/B (the real edge test): champion vs challenger on shared candidates
    min_ab_samples: int = _i("MOTHER_MIN_AB_SAMPLES", 12)           # resolved shared pairs needed
    ab_edge_margin: float = _f("MOTHER_AB_EDGE_MARGIN", 0.005)      # challenger must beat champ mean drift by this


@dataclass
class MasteryWeights:
    profit_factor: float = 0.22
    win_rate: float = 0.15
    drawdown: float = 0.18          # uses (1 - maxDD)
    calibration: float = 0.20       # overconfidence is punished
    sample_size: float = 0.10
    expectancy: float = 0.15


# --------------------------------------------------------------------------- #
# Optional integrations (auto-detected). Presence of the key lights the tentacle.
# --------------------------------------------------------------------------- #
@dataclass
class Integrations:
    alpaca_key: str = _s("ALPACA_API_KEY", "") or _s("APCA_API_KEY_ID", "")
    alpaca_secret: str = _s("ALPACA_SECRET_KEY", "") or _s("APCA_API_SECRET_KEY", "")
    alpaca_paper: bool = _b("ALPACA_PAPER", True)
    finnhub_key: str = _s("FINNHUB_API_KEY", "")
    alphavantage_key: str = _s("ALPHAVANTAGE_API_KEY", "")
    fmp_key: str = _s("FMP_API_KEY", "")
    reddit_client_id: str = _s("REDDIT_CLIENT_ID", "")
    reddit_secret: str = _s("REDDIT_CLIENT_SECRET", "")
    telegram_token: str = _s("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = _s("TELEGRAM_CHAT_ID", "")
    anthropic_key: str = _s("ANTHROPIC_API_KEY", "")
    # The kid decides often and needs speed -> Sonnet. The mother reviews rarely and
    # deeply -> Opus. Both go through the `claude` CLI (subscription) by default, so
    # these are passed as --model; they are also the API fallback model IDs.
    kid_model: str = _s("TENTACLE_KID_MODEL", "claude-sonnet-4-6")
    mother_model: str = _s("TENTACLE_MOTHER_MODEL", "claude-opus-4-8")
    # Optional Supabase mirror, so the bot's decisions/trades surface on the Bloomberg terminal
    # (see TERMINAL.md). Off unless both url + key are present. Writes go to PREFIXED tables
    # (default tentacle_*) so they never collide with another bot sharing the same project.
    supabase_url: str = _s("SUPABASE_URL", "")
    supabase_key: str = _s("SUPABASE_SERVICE_ROLE_KEY", "") or _s("SUPABASE_SERVICE_KEY", "")
    supabase_prefix: str = _s("TENTACLE_SUPABASE_PREFIX", "tentacle_")

    @property
    def alpaca_data_base(self) -> str:
        return "https://data.alpaca.markets"

    @property
    def alpaca_trade_base(self) -> str:
        return ("https://paper-api.alpaca.markets" if self.alpaca_paper
                else "https://api.alpaca.markets")


# --------------------------------------------------------------------------- #
# One assembled config object
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    contact_email: str = CONTACT_EMAIL
    broker: str = BROKER
    starting_capital: float = STARTING_CAPITAL
    gate: GateConfig = field(default_factory=GateConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    mastery: MasteryWeights = field(default_factory=MasteryWeights)
    mother: MotherConfig = field(default_factory=MotherConfig)
    integrations: Integrations = field(default_factory=Integrations)
    ladder: list = field(default_factory=build_ladder)


_CONFIG: Config | None = None


def get_config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config()
    return _CONFIG
