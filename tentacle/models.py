"""Core data models. These are the contracts every other module speaks.

Flow of a signal through the system:

    Source.poll() -> RawSignal
        -> classifier -> ClassifiedSignal  (ticker + catalyst category)
        -> confirmation -> Candidate        (one ticker+catalyst, >=2 families)
        -> confidence/judge/risk -> Decision (fire | shadow | veto)
        -> broker -> Trade -> Position
        -> learning -> Outcome (after the hold horizon resolves)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .util import now_iso, stable_id


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Tier(int, Enum):
    """Trust tiers. Lower number = closer to the source = higher trust."""

    PRIMARY = 1        # SEC filings, newswires, FDA, index changes, halts
    NEWS_API = 2       # benzinga ws, finnhub, fmp, alphavantage, gdelt, gnews
    SOCIAL = 3         # apewisdom, stocktwits, reddit, X-via-rss  (confirm only)
    FLOW = 4           # unusual whales, congress trades
    EXPANDED = 5       # crowd-forecast / dev-ecosystem / demand / gov / alt


class SourceFamily(str, Enum):
    """Independent signal *families*. Confirmation requires >=2 DISTINCT families
    so that two wires echoing the same press release do not count as two."""

    SEC_FILING = "sec_filing"
    NEWSWIRE = "newswire"
    REGULATORY = "regulatory"          # FDA, Federal Register
    INDEX = "index"                    # S&P / Nasdaq add / delete
    HALT = "halt"
    EARNINGS = "earnings"              # finnhub surprises / estimates
    NEWS = "news"                      # gdelt, google news, AV, FMP news
    SOCIAL = "social"                  # reddit / stocktwits / apewisdom / X-rss
    FLOW = "flow"                      # unusual whales, congress
    CROWD_FORECAST = "crowd_forecast"  # polymarket, kalshi, trends, wiki, HN
    DEV_ECOSYSTEM = "dev_ecosystem"    # github, pypi/npm/docker, hf, arxiv
    DEMAND = "demand"                  # appstore, steam, cloudflare, hiring
    GOV = "gov"                        # usaspending, clinicaltrials, uspto, fcc
    ALT = "alt"                        # adsb corporate jets


class Catalyst(str, Enum):
    """Catalyst categories. Each carries a learned drift prior + hold horizon."""

    EARNINGS_BEAT = "earnings_beat"          # post-earnings-announcement drift
    INDEX_INCLUSION = "index_inclusion"      # mechanical forced buying
    FDA_APPROVAL = "fda_approval"            # binary biotech
    MNA_TARGET = "mna_target"                # takeover target
    GOV_CONTRACT = "gov_contract"            # awarded federal contract
    ACTIVIST_STAKE = "activist_stake"        # 13D / >5% activist
    INSIDER_CLUSTER = "insider_cluster"      # Form 4 cluster buying
    AI_QUANTUM_BREAKOUT = "ai_quantum_breakout"
    UNKNOWN = "unknown"                       # does not classify -> never fires


class DecisionType(str, Enum):
    FIRE = "fire"        # real (or paper) order placed
    SHADOW = "shadow"    # logged, no order, the default, expected output
    VETO = "veto"        # would have fired but a hard gate blocked it


class TradeStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class OutcomeLabel(str, Enum):
    WIN = "win"
    LOSS = "loss"
    FLAT = "flat"
    PENDING = "pending"


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #
@dataclass
class RawSignal:
    """One item from one tentacle, before classification."""

    source_id: str
    family: str                       # SourceFamily value
    tier: int                         # Tier value
    title: str
    url: str = ""
    summary: str = ""
    tickers: list[str] = field(default_factory=list)
    event_time: Optional[str] = None  # ISO; when the event actually happened
    fetched_at: str = field(default_factory=now_iso)
    base_reliability: float = 0.5     # source default weight (learning tunes it)
    signal_strength: float = 0.5      # 0..1 this item's own intensity
    catalyst_hint: Optional[str] = None
    dedup_key: str = ""
    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.tickers = [t.upper().strip() for t in self.tickers if t and t.strip()]
        if not self.dedup_key:
            self.dedup_key = stable_id(
                self.source_id, self.url or self.title, ",".join(sorted(self.tickers))
            )

    @property
    def ticker(self) -> Optional[str]:
        return self.tickers[0] if self.tickers else None


@dataclass
class ClassifiedSignal:
    """A RawSignal pinned to ONE ticker + ONE catalyst category."""

    raw: RawSignal
    ticker: str
    catalyst: str                     # Catalyst value
    classify_confidence: float = 0.5  # 0..1 how sure the classifier is
    rationale: str = ""

    @property
    def family(self) -> str:
        return self.raw.family

    @property
    def tier(self) -> int:
        return self.raw.tier


# --------------------------------------------------------------------------- #
# Candidate (a tradeable hypothesis)
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    """One ticker + one catalyst, supported by a bundle of classified signals."""

    ticker: str
    catalyst: str
    signals: list[ClassifiedSignal] = field(default_factory=list)
    first_event_time: Optional[str] = None
    last_event_time: Optional[str] = None

    # scoring (filled by the brain)
    confidence: float = 0.0           # 0..100
    raw_score: float = 0.0
    size_fraction: float = 0.0        # 0..1 of the CURRENT RUNG's capital
    size_dollars: float = 0.0
    pillar_scores: dict = field(default_factory=dict)
    judge_notes: str = ""

    # the LLM decision-maker's verdict on this candidate: "buy" | "pass" | "".
    # "" means it was never put to the decider (e.g. did not pass confirmation, or
    # was outside the bounded top-N this cycle); the engine fires only on "buy".
    verdict: str = ""

    # decision (filled by risk + engine)
    decision: str = DecisionType.SHADOW.value
    reasons: list[str] = field(default_factory=list)

    @property
    def families(self) -> set[str]:
        return {s.family for s in self.signals}

    @property
    def independent_families(self) -> int:
        return len(self.families)

    @property
    def source_ids(self) -> set[str]:
        return {s.raw.source_id for s in self.signals}

    def cid(self) -> str:
        """Canonical candidate id. The SAME formula is used by the candidates table,
        the decision journal, the A/B shadow log, and outcomes, so they all join."""
        return stable_id(self.ticker, self.catalyst, self.first_event_time or "",
                         ",".join(sorted(self.source_ids)))

    def add_reason(self, msg: str) -> None:
        self.reasons.append(msg)


# --------------------------------------------------------------------------- #
# Account / risk state
# --------------------------------------------------------------------------- #
@dataclass
class AccountState:
    """Snapshot of where we stand right now."""

    rung: int = 0
    rung_capital: float = 0.0         # capital allocated at this rung
    equity: float = 0.0               # rung_capital + open P&L
    cash: float = 0.0                 # total cash in the rung
    settled_cash: float = 0.0         # cash free to redeploy w/o a GFV
    unsettled_cash: float = 0.0       # T+1 pending
    open_positions: int = 0
    realized_pnl_today: float = 0.0
    armed: bool = False               # operator must ARM before real fires
    alive: bool = True                # False == the bot has DIED (ruin floor)
    halted_today: bool = False        # daily-loss circuit breaker tripped


# --------------------------------------------------------------------------- #
# Trades / positions / outcomes
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    ticker: str
    qty: float
    entry_price: float
    size_dollars: float
    confidence: float
    catalyst: str
    rung: int
    horizon_hours: float
    stop_price: float
    target_price: Optional[float] = None
    entry_time: str = field(default_factory=now_iso)
    paper: bool = True
    status: str = TradeStatus.OPEN.value
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    pnl: Optional[float] = None
    outcome: str = OutcomeLabel.PENDING.value
    candidate_id: str = ""
    broker_order_id: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = stable_id(self.ticker, self.entry_time, self.catalyst)


@dataclass
class Outcome:
    """Resolved result of a fired OR shadow signal, used by the learning loop."""

    candidate_id: str
    ticker: str
    catalyst: str
    confidence: float
    fired: bool                       # True == real/paper trade, False == shadow
    entry_price: float
    horizon_hours: float
    exit_price: Optional[float] = None
    drift_pct: Optional[float] = None  # realized move over the horizon
    label: str = OutcomeLabel.PENDING.value
    source_ids: list[str] = field(default_factory=list)
    families: list[str] = field(default_factory=list)
    resolved_at: Optional[str] = None
    created_at: str = field(default_factory=now_iso)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = stable_id(self.candidate_id, self.ticker, self.created_at)
