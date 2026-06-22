# TENTACLE module contracts

This file is the API spec every module implements. Read the foundation first:
`tentacle/models.py`, `tentacle/config.py`, `tentacle/storage.py`,
`tentacle/marketdata.py`, `tentacle/sources/base.py`, `tentacle/sources/registry.py`,
and the reference tentacle `tentacle/sources/sec_edgar.py`.

GLOBAL RULES for every file:
- Python 3.14. Run tests with the repo venv: `.venv/bin/python`.
- NEVER edit foundation files (models/config/storage/util/marketdata/sources/base.py/
  sources/registry.py/sources/sec_edgar.py/sources/__init__.py). Only create the files
  assigned to you (and the `__init__.py` of any NEW subpackage you own).
- NO em dashes anywhere (not in code, comments, or docstrings). Use periods, commas,
  colons, parens, or "and". This is a hard rule.
- Network/parse errors must be swallowed (return None / neutral / empty), never raised,
  so one flaky feed never crashes the loop. Treat unknown data as "unknown" and FAIL SAFE
  (a missing price/regime must BLOCK a fire, never allow one by accident).
- Honest comments. If something is a proxy/approximation, say so.
- Keep imports light. `import pandas`/`numpy`/`yfinance` only inside functions that need them,
  guarded with try/except so the core still imports if a heavy dep is missing.

## Catalyst categories (from models.Catalyst)
earnings_beat, index_inclusion, fda_approval, mna_target, gov_contract,
activist_stake, insider_cluster, ai_quantum_breakout, unknown (never fires).

## Source families (models.SourceFamily) and the WEIGHTING LAW
Trigger-allowed families (may be the catalyst that fires a trade):
  sec_filing, newswire, regulatory, index, halt, earnings, news.
Confirm-only families (NEVER fire alone, pump risk):
  social, flow, crowd_forecast, dev_ecosystem, demand, gov, alt.
(See config.TRIGGER_FAMILIES / CONFIRM_ONLY_FAMILIES.)

---

# SOURCES (tentacles)
Each is a `Source` subclass (see base.py). Set class attrs `id` (unique, snake_case),
`family` (a SourceFamily value), `tier` (a Tier value), `requires_keys` (tuple of env var
names, empty for free), optionally `base_reliability` and `min_interval_seconds`. Implement
`fetch(self) -> Iterable[RawSignal]` yielding EVERYTHING currently visible; the base class
dedups, persists, and weights. Set `tickers=[...]`, `event_time`, `signal_strength` (0..1),
`catalyst_hint` (a Catalyst value or None), `url`, `summary`, `raw={...}`.
Use `tentacle.util.parse_rss(url)`, `http_json(url, params=, headers=)`, `http_text(url)`,
`extract_tickers(text, aggressive=False)`. Read keys from `self.cfg.integrations`.
A source that maps to NO ticker should yield nothing (we only trade mapped US tickers),
EXCEPT confirm-only thematic sources may map to a configured theme->ticker basket.

The watchlist for per-ticker sources: read env `TENTACLE_WATCHLIST` (comma-sep) via a small
helper; default to a basket of liquid names + recent signal tickers from
`storage.recent_signals`. Keep it <= 60 tickers to stay polite.

---

# BRAIN  (package `tentacle/brain/`, you own brain/__init__.py and brain/pillars/__init__.py)

## brain/classifier.py
`def classify(raw: RawSignal, cfg=None) -> list[ClassifiedSignal]`
- One ClassifiedSignal per ticker in `raw.tickers` (usually 1; skip if no tickers).
- Category resolution order: (1) `raw.catalyst_hint` if set; (2) keyword rules over
  `title+summary` (e.g. "fda approval"->fda_approval, "to acquire/merger"->mna_target,
  "added to s&p/index"->index_inclusion, "beats/tops estimates"->earnings_beat,
  "awarded ... contract"->gov_contract, "13d/activist"->activist_stake, "insider buy"->
  insider_cluster, "launches model/open-source/benchmark" + AI terms->ai_quantum_breakout);
  (3) family default; (4) `unknown`.
- `classify_confidence` 0..1 (hint=0.8, strong keyword=0.7, family default=0.4, unknown=0.1).

## brain/confirmation.py
`def build_candidates(classified: list[ClassifiedSignal], cfg=None) -> list[Candidate]`
- Group by (ticker, catalyst). Build one Candidate per group, attaching all signals,
  setting first/last_event_time (min/max of signal event_times). Return ALL groups
  (even single-signal ones, so shadow logging covers them). Do NOT decide fire here.
`def passes_confirmation_gate(cand: Candidate, cfg=None) -> tuple[bool, list[str]]`
- True only if: >= cfg.gate.min_sources independent source_ids AND >= cfg.gate.min_families
  distinct families AND at least one of those families is in config.TRIGGER_FAMILIES AND the
  signals fall within cfg.gate.confirm_window_min minutes of each other. Return reasons
  either way (used for shadow logging).

## brain/confidence.py
`def score_candidate(cand: Candidate, cfg=None, storage=None, md=None) -> Candidate`
Set `cand.raw_score`, `cand.confidence` (0..100), `cand.size_fraction`
(via `config.size_fraction_for`), `cand.pillar_scores` (dict). Formula (implement exactly):
```
group signals by family; per family f:
  best_f = max(s.raw.base_reliability * s.raw.signal_strength for s in f)
  rest_f = sum(...) - best_f
  family_score_f = best_f + 0.3*rest_f          # within-family correlation discount
total_raw = sum(family_score_f over families)
import math; base = 100*(1 - math.exp(-0.9*total_raw))
prior = storage.get_catalyst_prior(cand.catalyst) or config.catalyst_spec(cat).drift_prior
age_h = hours since cand.first_event_time (0 if unknown)
earliness = clamp(1.2 - 0.1*age_h, 0.6, 1.2)
magnitude = 1.0   # v1: reference price at event not tracked; documented proxy
social_score = sum family_score_f for families in CONFIRM_ONLY_FAMILIES
crowding_penalty = 15 * (social_score/total_raw if total_raw>0 else 0)
staleness_penalty = 0 if age_h<=24 else min(20, (age_h-24)*0.5)
score = clamp(base*prior*earliness*magnitude - crowding_penalty - staleness_penalty, 0, 100)
```
Also apply pillar adjustments (see judge/pillars): if a pillar veto is set, score may be cut.
Keep `clamp(x,lo,hi)` as a local helper.

## brain/pillars/technical.py
`def technical_read(ticker: str, cfg=None, md=None) -> dict`
Return `{"score": -1..1, "veto": bool, "note": str}`. Use md.daily_bars(ticker, 60).
Veto True when price is extended at the highs into a bearish reversal (e.g. RSI(14) > 75
AND last candle closes red below prior close near a 60-day high). Neutral (score 0, veto False)
when data unavailable.

## brain/pillars/options_flow.py
`def options_flow_read(ticker: str, cfg=None) -> dict`
Return `{"score": -1..1, "note": str}`. No clean free realtime options feed; default neutral
(score 0). If a future flow source tags this ticker, reflect it. Document the limitation.

## brain/pillars/fundamentals.py
`def fundamentals_read(ticker: str, cfg=None, md=None) -> dict`
Return `{"score": -1..1, "squeeze_boost": 0..0.15, "note": str}`. Best-effort via yfinance
`Ticker(ticker).info` (guarded): small float + high short interest -> positive squeeze_boost;
negative free cash flow + high cash burn -> small negative score (trust catalyst less).
Neutral when unavailable.

## brain/judge.py
`def judge_candidates(cands: list[Candidate], cfg=None, top_n=None, md=None, storage=None) -> list[Candidate]`
- Sort by confidence desc, take top_n (default cfg.engine.max_candidates_to_judge).
- Require >= 2 ORTHOGONAL evidence TYPES (count distinct families + each non-neutral pillar as
  a type). If < 2, append a reason and set decision to "shadow" (do not fire).
- Run pillars (technical/options/fundamentals); fold into cand.pillar_scores; a technical veto
  sets a "technical_veto" reason and should knock confidence down hard (e.g. *0.5).
- If `cfg.integrations.anthropic_key`: call Claude as a final analyst on these few candidates
  (bounded). Use the Anthropic Messages API via `requests` (model "claude-sonnet-4-6", small
  max_tokens, JSON-ish response: keep/cut + one-line reason). Set `cand.judge_notes`. On any
  error fall back to the heuristic. If no key, heuristic only.
- Return the (possibly re-scored) candidates. Do NOT place orders here.

---

# RISK  (package `tentacle/risk/`, you own risk/__init__.py)
TENTACLE keeps its OWN internal capital ledger (the rung). The broker balance is NOT the
bot's capital. AccountState is derived from storage (ledger + open trades + current rung),
using md for unrealized P&L. The broker is only an execution + price venue.

## risk/regime.py
`def regime_is_up(cfg=None, md=None) -> tuple[bool, str]`
True when benchmark (cfg.regime.benchmark, default SPY) fast MA >= slow MA on daily closes.
Returns (ok, note). If data unavailable -> (False, "regime unknown") FAIL SAFE.

## risk/caps.py  (pure helpers)
`def per_trade_stop_price(entry: float, cfg=None) -> float`  (entry*(1-stop_pct))
`def per_trade_target_price(entry: float, cfg=None) -> float`
`def daily_loss_breached(account: AccountState, cfg=None) -> bool`  (realized_pnl_today <= -daily_loss_halt_pct*rung_capital)
`def ruin_breached(equity: float, stage_start: float, cfg=None) -> bool`  (equity <= stage_start*(1-ruin_floor_pct))

## risk/cash.py  (T+1 settled-cash / GFV)
`def available_settled_cash(account: AccountState) -> float`
`def would_cause_gfv(amount: float, account: AccountState, cfg=None) -> bool`
  (only relevant if cfg.risk.cash_account; buying with unsettled proceeds risks a GFV)
`def settle_due(storage, cfg=None) -> float` (wrap storage.settle_due with today's date)

## risk/sizing.py
`def position_size(score: float, account: AccountState, cfg=None, md=None, ticker=None, price=None) -> tuple[float, float]`
Returns (dollars, qty). dollars = size_fraction_for(score) * account.rung_capital, capped by
cfg.risk.max_position_fraction*rung_capital, by available settled cash (cash account), and by a
fractional-Kelly ceiling (cfg.risk.kelly_fraction_cap). qty = floor-ish dollars/price (price
from arg or md.last_price). If price unknown -> (0,0).

## risk/manager.py
```
class RiskManager:
    def __init__(self, cfg=None, storage=None, md=None, broker=None)
    def account_state(self) -> AccountState
        # rung + rung_capital from storage kv ("rung","stage_start_equity"); equity = stage_start
        #   + realized P&L (closed trades this stage) + unrealized (open trades via md);
        #   cash/settled/unsettled from storage; armed from cfg.engine.armed or kv "armed";
        #   alive from kv "alive" (default True); halted_today from kv.
    def market_gates(self, cand: Candidate) -> tuple[bool, list[str]]
        # market open (md.is_market_open) if cfg.gate.require_market_open;
        # regime up if cfg.gate.require_uptrend_regime; earliness: current price not more than
        #   cfg.gate.earliness_max_runup above the event-day reference (use md daily bar open of
        #   the event date as a proxy; if unknown, do not block on earliness but note it);
        # liquidity floor (md.avg_dollar_volume >= cfg.gate.liquidity_min_dollar_vol and price
        #   >= liquidity_min_price); no-chase vs VWAP (price <= vwap*(1+no_chase_vwap_band)).
        # Return (ok, reasons). FAIL SAFE on unknown price.
    def evaluate(self, cand: Candidate, account: AccountState) -> tuple[bool, list[str]]
        # ALL must hold: account.alive, account.armed, not account.halted_today,
        # not daily_loss_breached, not ruin_breached, open_positions < max_open_positions,
        # not storage.has_open_position(ticker), confirmation gate (call
        # brain.confirmation.passes_confirmation_gate), confidence size band > 0, market_gates.
        # Return (allow_fire, reasons). This is the LAW; the model cannot override it.
```

---

# LADDER  (package `tentacle/ladder/`, you own ladder/__init__.py)

## ladder/mastery.py
`def compute_mastery(storage, cfg=None, rung=None) -> tuple[float, dict]`
Use resolved outcomes + closed trades for the current stage. Components (each 0..1, then
weighted by config.MasteryWeights, scaled to 0..100):
  profit_factor (gross win / gross loss, mapped via x/(x+1) style to 0..1),
  win_rate, drawdown (1 - maxDD on the stage equity curve), calibration
  (1 - calibration_error from learning.calibration), sample_size (saturating: n/(n+threshold)),
  expectancy (avg R per trade mapped to 0..1). Return (score0..100, components dict).
Handle empty history -> (0.0, {...}).

## ladder/promotion.py
`def evaluate_ladder(storage, cfg=None, md=None) -> dict`
Read current rung from kv "rung" (default -1). Compute mastery + sample count. Compare to the
Rung.promote_mastery / promote_samples for the current rung (config.rung_by_level). Also check
DEATH: if ruin_breached for the stage -> action "death" (set kv alive False, demote one rung,
reset stage_start_equity to the lower rung's capital, require re-qualify). Return
`{"rung", "mastery", "samples", "action": "hold|promote|demote|death", "new_rung", "note"}`.
Promotion sets kv "rung" and resets "stage_start_equity" to the new rung capital. Never auto
self-restart from death beyond demoting one rung (re-qualify from a lower rung).

---

# LEARNING  (package `tentacle/learning/`, you own learning/__init__.py)

## learning/outcomes.py
`def log_outcome(cand: Candidate, entry_price: float, fired: bool, storage, cfg=None) -> str`
Create a PENDING Outcome (models.Outcome) for this candidate (BOTH fired trades AND shadow
signals so the bot learns from the road not taken). Store source_ids/families. Return id.
`def resolve_due_outcomes(storage, md=None, cfg=None) -> int`
For each pending outcome whose horizon (config.catalyst_spec(cat).horizon_hours from created_at)
has elapsed: fetch current price (md.last_price), compute drift_pct = (price-entry)/entry,
label win/loss/flat (flat band +-1%), save. Return count resolved.

## learning/calibration.py
`def calibration_error(storage, cfg=None) -> float`
Bucket resolved outcomes by predicted confidence (deciles), compare predicted win-prob
(confidence/100) to realized win rate; return mean abs gap 0..1. Empty -> 0.5 (unknown).

## learning/tuner.py
`def retune(storage, cfg=None) -> dict`
From resolved outcomes: per source_id, win rate of outcomes it contributed to -> nudge
storage.set_source_weight tuned_weight toward (0.5 + winrate) bounded [0.3, 1.5] with slow EMA.
Per catalyst, update storage.upsert_catalyst_prior drift_prior toward a value derived from avg
drift bounded [0.7, 1.3]. Require a minimum sample (e.g. >=8) before moving anything. Return a
summary dict of what changed.

---

# BROKER  (package `tentacle/broker/`, you own broker/__init__.py)

## broker/base.py
```
@dataclass class BrokerOrder: order_id:str; ticker:str; qty:float; side:str;
    filled_price:float; status:str; raw:dict=field(default_factory=dict)
class Broker(ABC):
    def is_market_open(self) -> bool
    def last_price(self, ticker: str) -> Optional[float]
    def get_account(self) -> dict     # {equity, cash, settled_cash, unsettled_cash, buying_power}
    def list_positions(self) -> list[dict]
    def get_position(self, ticker: str) -> Optional[dict]
    def submit_buy(self, ticker: str, qty: float, *, limit: Optional[float]=None) -> BrokerOrder
    def close_position(self, ticker: str) -> BrokerOrder
```
LONG ONLY. No options, no crypto, no shorting. submit_buy only; selling is via close_position.

## broker/simulator.py  `class SimulatorBroker(Broker)`
Internal paper fills using marketdata for the reference price, applying realistic slippage
(model "your fill lands AFTER the spike": buy fills at price*(1+slip), slip default 0.15%).
Tracks cash/positions in storage kv ("sim_cash","sim_positions"). Uses md for last_price and
is_market_open. Seed sim_cash lazily from the current rung capital.

## broker/alpaca.py  `class AlpacaBroker(Broker)`
Real adapter over the Alpaca REST API using `requests` (no SDK). Trade base from
cfg.integrations.alpaca_trade_base (paper by default), data via marketdata. Headers
APCA-API-KEY-ID / APCA-API-SECRET-KEY. submit_buy = market day order, qty (supports
fractional via "notional" if qty*price small). Map Alpaca account fields to the dict
(cash, non-marginable/settled via "cash" and "pending_transfer_in"? use "cash" as settled and
0 unsettled if not available). Guard everything.

## broker/robinhood.py  `class RobinhoodBroker(Broker)`
Stub: each method raises NotImplementedError with a clear message pointing to robin_stocks,
EXCEPT is_market_open/last_price which can delegate to marketdata. Documented as a future path.

## broker/factory.py
`def get_broker(cfg=None, storage=None, md=None) -> Broker`
Pick by cfg.broker: "alpaca" -> AlpacaBroker if keys present else SimulatorBroker (warn);
"simulator" -> SimulatorBroker; "robinhood" -> RobinhoodBroker. Default SimulatorBroker.

---

# BACKTEST  (package `tentacle/backtest/`, you own backtest/__init__.py)

## backtest/gauntlet.py
`def run_gauntlet(cfg=None, tickers=None, years=3) -> dict`
Rung -1 gate. A full free news backtest is infeasible, so do an HONEST proxy that actually
validates the core drift hypothesis and the risk math:
  1) EARNINGS DRIFT (PEAD): for a basket (tickers or a default ~30 liquid names), use yfinance
     `Ticker(t).get_earnings_dates()` (guarded) to find past earnings; measure forward drift
     over the earnings horizon; compute win rate / avg drift / profit factor as if we had
     entered post-announcement. Seed catalyst_priors for earnings_beat from the result.
  2) RISK/SIZING MONTE CARLO: simulate N synthetic signal streams with the confidence->size
     bands and the stop/target/ruin rules to confirm the engine cannot blow past the ruin floor
     and that expectancy is positive given a plausible edge. 
Return `{"mastery_estimate", "earnings_drift": {...}, "montecarlo": {...}, "passed": bool,
"notes": [...]}`. Clearly log what is real vs proxied. `passed` = mastery_estimate>=80 with
sufficient samples (per the rung -1 rule). Do not overstate; this seeds priors and sanity-checks
the math, it is not a claim of future returns.
```
