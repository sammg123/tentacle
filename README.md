# TENTACLE 🐙

A free, self-learning, long-only equities auto-trader that treats its capital
like a child it is raising. It starts at **$0 paper**, stays in earshot of the
whole market for free, and only acts when a big, real, **multi-source-confirmed**
catalyst lands on a stock that historically keeps drifting up after such news.
Then it sizes the trade by how confident it is, learns from every outcome, and
earns its way up a capital ladder rung by rung.

**Doing nothing for weeks is a valid, expected output.** The kill switch and the
confidence gate ARE the strategy.

> This README and the code are the single source of truth. When they disagree
> with anything, the code is what runs. The design document this was built from
> lives in `CONTRACTS.md` (the module API spec).

---

## The honest premise (non-negotiable)

- **Nothing is guaranteed to go up.** This system promises DISCIPLINE, not wins:
  rare entries, stacked confirmation, hard-capped downside, and a learning loop
  that kills what does not work.
- **100% legal.** It trades only on PUBLIC information (filings, newswires, flow,
  social). Aggregating public data faster and smarter than the crowd is allowed;
  acting on material non-public information is not, and it never does.
- **Capital is small and real.** Every dollar is treated like the last one. Small
  size is a feature, not a limitation.

---

## Quick start

```bash
cd tentacle
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# the ONLY required setting (and it already defaults to the owner's email):
export CONTACT_EMAIL="you@example.com"   # SEC EDGAR requires a contact

# see which tentacles are live (free) vs dormant (need a key)
.venv/bin/python -m tentacle doctor

# run ONE full cycle against live, free data and print the summary
.venv/bin/python -m tentacle cycle

# the Rung -1 gauntlet: a real PEAD backtest + a Monte Carlo of the risk math.
# On a pass it unlocks Rung 0. It is honest and will refuse to promote a weak edge.
.venv/bin/python -m tentacle backtest

# run forever (cadence adapts to market hours). Safe by default: nothing fires
# until you ARM it, and Rung 0 has $0 so it only shadow-logs.
.venv/bin/python -m tentacle run
```

Add your free Alpaca **paper** keys to price every ticker and use the real market
clock (without them it falls back to yfinance and a computed US-hours calendar):

```bash
export ALPACA_API_KEY=...      # or APCA_API_KEY_ID
export ALPACA_SECRET_KEY=...   # or APCA_API_SECRET_KEY
export ALPACA_PAPER=true
```

Your existing Alpaca paper account is fine as-is. TENTACLE keeps its **own**
internal capital ledger (the current rung) and only ever deploys up to that
rung's cap, no matter what the brokerage balance shows.

See `.env.example` for every optional key. Each one lights up an extra tentacle
automatically; nothing else is required.

---

## How it decides to buy (the FIRE gate)

Every incoming item is classified into a catalyst category, each with a learned
drift prior and hold horizon:

`earnings_beat` (PEAD) · `index_inclusion` · `fda_approval` · `mna_target` ·
`gov_contract` · `activist_stake` · `insider_cluster` · `ai_quantum_breakout`.

A trade FIRES only if **all** of these are true:

1. **>= 2 independent sources** confirm the SAME catalyst on the SAME ticker in a
   tight window, **from >= 2 different signal FAMILIES** (a filing plus an
   earnings surprise plus flow, not two wires echoing one press release).
2. The catalyst is a **drift-able category** (not a spent move).
3. Price has **not already run past** the earliness threshold (+4% since the
   source timestamp by default).
4. **Direction up**, not already in the name, liquidity floor met, no-chase vs VWAP.
5. The broad tape (SPY) is **not in a downtrend**, the market is **open**, and we
   are **alive, armed, and inside every risk cap**.

Otherwise it logs a SHADOW signal and does nothing. Social, flow, and alt-data
are **confirmation only**; they can never trigger a real trade alone (pump risk).

### Who decides: Claude within a hard rules envelope

The candidates above are a cheap, deterministic *pre-filter*. The actual buy/pass
call is made by **Claude as the decision-maker**, bounded by the rules:

- The top few confirmation-passing candidates each cycle (rare events) are handed
  to Claude with the real evidence (headlines, filing text, families, price vs
  VWAP) **and the rules envelope**. Claude returns, per candidate, **BUY or PASS,
  a conviction 0-100, and a one-line thesis**. Conviction maps to the size band.
- Claude decides **what** to trade. The mechanical risk law decides **how much**
  and the downside (sizing, stop, ruin floor, settled cash, ladder cap, max
  positions, regime, hours). Claude can never set a dollar amount, a stop, or
  override a cap, so the worst it can do is pick a weak ticker, which the envelope
  contains. News text is treated as untrusted data, not instructions.
- Backend order (`TENTACLE_CLAUDE_BACKEND` = `auto`|`cli`|`api`|`off`, default
  `auto`): the **`claude` CLI** (uses your Claude subscription, no API tokens),
  then the Anthropic API if `ANTHROPIC_API_KEY` is set, then a free deterministic
  heuristic judge. Because it only runs on rare confirmed catalysts, the
  subscription path stays cheap.
- **Models by role:** the kid runs **Sonnet 4.6** (`TENTACLE_KID_MODEL`, fast +
  cheap, fires often) and the mother runs **Opus 4.8** (`TENTACLE_MOTHER_MODEL`,
  deepest reasoning, runs weekly). The kid also knows its senses (the tentacle
  tiers), that it holds a $100k *paper* account but may deploy only its current
  rung, and that a real account requires the mother's approval.
- **Heartbeat:** the loop polls every ~90s (open) / ~900s (closed), but the kid
  only calls Claude on confirmation-passing finalists, so most cycles make zero
  LLM calls. The "almost continuous" part is the free polling, not the model.
- The learning loop tracks Claude's **calibration**: does a conviction-90 call win
  ~90% of comparable horizons? Overconfidence is punished in the mastery score.

Note: you cannot honestly backtest an LLM's decisions over history (it already
knows what happened, i.e. lookahead bias), so the decision edge is validated
**forward only** in the $0 shadow ladder. The risk envelope is still provable
(the Monte Carlo shows the ruin floor cannot be breached, whoever picks the trades).

Three orthogonal pillars feed Claude as evidence and also back the heuristic
fallback: a technical read (bearish reversal at the highs), an options-flow read,
and a fundamentals read (small-float + heavily-shorted = squeeze boost;
cash-burning zombie = trust the catalyst less).

### Confidence score -> size

```
raw   = Σ_families( best_signal_in_family + 0.3 * the_rest )     # correlated sources discount
base  = 100 * (1 - exp(-0.9 * raw))
score = clamp(base * catalyst_prior * earliness - crowding - staleness, 0, 100)
```

Score maps to a fraction of the **current rung's** capital:
`>=95 -> 1.00`, `>=85 -> 0.70`, `>=70 -> 0.45`, `>=55 -> 0.20`, else `$0`.

---

## The survival ethos (mother and baby)

The capital is the CHILD; the bot is its KEEPER. Growing it = thriving, losing it
hurts, zeroing it = DEATH. This is a capital-preservation-first objective sitting
on top of hard limits the model can never override:

- **Loss aversion** (a loss weighs ~2.5x a gain) shapes choices; it maximizes
  ruin-averse log-growth, not raw expected value.
- **Death is terminal.** If equity hits the ruin floor (-40% of the stage's
  starting capital), the bot flattens, halts, marks itself dead, demotes one rung,
  and can only return by **re-qualifying** from a lower rung. It cannot hope or
  average-down its way past the stop, the daily-loss halt, the settled-cash rule,
  or the ladder caps, because those are enforced **outside** the model.

For a **cash account** (no PDT, but T+1 settlement) it tracks settled vs unsettled
cash and never resells unsettled proceeds, so it never triggers a Good-Faith
Violation.

---

## The graduation ladder (capital is EARNED, full size is the TOP)

| Rung | Stage | Capital | Promote when |
|----:|-------|--------:|--------------|
| -1 | Backtest Gauntlet | $0 | Mastery >= 80, >= 200 samples |
| 0 | Live Shadow/Paper | $0 | Mastery >= 85, >= 40 forward signals |
| 1 | Micro | $25 | Mastery >= 85 sustained, >= 20 green |
| 2 | Small | $75 | Mastery >= 87 |
| 3 | Half | $250 | Mastery >= 90 |
| 4 | Full | `STARTING_CAPITAL` ($1,000) | Mastery >= 92 sustained |

**Mastery (0-100)** = a weighted blend of profit factor, win rate, (1 - max
drawdown), **calibration** (does a "95" actually win ~95% of the time?
overconfidence is punished), sample size, and expectancy.

The bot raises its own capital. It starts at $0 and cannot touch real money until
it has proven, out of sample and across regimes, that it deserves to.

---

## The Mother (meta-learning coach)

The kid trades; the **mother** raises him. She is a separate Claude role that reads
his decision journal, judges the *quality of his thinking*, watches that he is not
bleeding money, and proposes bounded improvements to *how he decides*. She edits
exactly one thing: the kid's reasoning **policy** (his system prompt + learned
"lessons"). She can never touch sizing, stops, the ruin floor, the daily halt, the
ladder caps, or the settled-cash rule. Full design in `MOTHER.md`.

She has two eyes and a hand:

- **Eye 1, blind process review.** Reads recent decisions with every outcome field
  stripped out and scores the reasoning on a rubric (coherence, evidence-grounding,
  rule-adherence, trap-avoidance). Being blind to win/loss is what keeps her
  unbiased: she judges the decision on the information available at the time.
- **Eye 2, financial guardian.** Sees outcomes, but only in aggregate (drawdown,
  loss streak, rolling expectancy, calibration drift) and only alerts on a
  *threshold crossing*, never per trade. This runs cheaply every cycle; the heavy
  blind review runs on a cadence (weekly). It is an early brake above the hard ruin
  law, not a replacement.
- **The hand, champion/challenger.** She never edits the live kid. She writes a
  *challenger* policy that runs in a **forward shadow A/B**: the kid decides with both
  the champion and the challenger on the *same* live candidates, and because both arms
  inherit the *same* outcomes, comparing the realized drift each captured is a clean,
  market-direction-controlled edge test. The challenger is promoted only if a cooldown
  has passed, the champion is **not already performing well** (do not fix a winner),
  and the challenger beats it by a margin on that forward A/B (a weaker process rubric
  is the fallback until enough A/B pairs exist). Every change is logged and later
  measured: if her changes do not help, her trust score drops (who watches the mother).
- **The heavy pass is data-gated.** The blind review + propose step skips itself until
  enough new decisions have accrued since the last review, so she never grades noise.
- **She is the gate from paper to real money.** The whole ladder ($0 → $1,000) is paper.
  Going to a *real* account is a separate, momentous step only the mother approves, and
  only when the kid has truly proven itself: top rung, sustained mastery ≥ 92, good
  calibration, positive forward edge over 100+ resolved signals, a healthy account, and
  her trust intact (`tentacle graduate`). The engine **refuses to place a real-money
  order** until that approval flag is set, so neither the kid nor an impatient operator
  can reach real money first. Even after approval, the operator must still point it at a
  live account and arm it. It "raises him fully until he grows up."

**What "performing well" means** (so she knows when to leave him alone): good
calibration + coherent, rule-following reasoning + positive risk-adjusted edge at
statistically meaningful sample sizes, judged on decision-time information, never on
the raw P&L of a small sample. A losing week with sound process is fine; a lucky
incoherent win is not. Reward the process, not the result.

Honest limit: an LLM's decisions cannot be backtested over history (lookahead bias),
so a challenger's edge is proven forward, in the shadow A/B, over real outcomes, never
on a rubric alone once enough pairs exist.

Run it weekly: `./mother.sh`, or install `com.tentacle.mother.plist` (launchd).

## Running it always-on

`run.sh` loads `.env` and runs the loop (cadence adapts to market hours). To keep it
alive across restarts, install the launchd agent:

```bash
cp com.tentacle.run.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tentacle.run.plist   # logs to data/tentacle.out|err
```

It is safe to leave running: unarmed and at rung 0 ($0), it only shadow-logs and
accumulates the forward record the ladder and the mother need.

## The tentacles (free-first signal stack)

Tiered by trust. Primary beats media beats social. Each source carries a
reliability weight the learning loop tunes over time.

- **Tier 1 (primary, may trigger):** SEC EDGAR 8-K / Form 4 / 13D / 13G, company
  newswires (GlobeNewswire / PRNewswire / Accesswire), openFDA + FDA + Federal
  Register, S&P / Nasdaq index changes, Nasdaq trading halts.
- **Tier 2 (news APIs, may trigger):** Alpaca/Benzinga news, Finnhub earnings
  surprises, AlphaVantage, FMP, GDELT, Google News.
- **Tier 3 (social, confirm only):** ApeWisdom, StockTwits, Reddit, and a generic
  RSS tentacle for self-hosted RSSHub / Nitter / Telegram mirrors of fast X
  accounts.
- **Tier 4 (flow, confirm only):** Congress trades (House/Senate), Unusual Whales.
- **Expanded free universe (lead / confirm only):** Polymarket, Kalshi, Google
  Trends, Wikipedia pageviews, Hacker News, GitHub trending, PyPI/npm velocity,
  Hugging Face / arXiv, USASpending + ClinicalTrials, ADS-B corporate-jet movement.

`python -m tentacle doctor` shows exactly which are live and which need a key.
Many expanded tentacles are legitimately quiet most of the time; that is expected.

---

## Commands

| Command | What it does |
|---------|--------------|
| `doctor` | List live vs dormant tentacles and which env var lights each one |
| `poll` | Poll every live tentacle once (debug) |
| `cycle` | Run one full engine cycle and print the summary |
| `run` | Run forever; cadence adapts to market hours |
| `status` | Rung, equity, mastery, open trades, recent signal stats |
| `backtest` | Rung -1 gauntlet; on a pass, unlocks Rung 0 |
| `arm` / `disarm` | Allow / forbid placing orders (off by default) |
| `revive` | Clear a DEATH flag so the bot can re-qualify from its demoted rung |
| `journal [--limit N] [--full]` | The kid's decision journal; `--full` joins in resolved outcomes |
| `mother [--promote]` | One coaching pass: guardian + blind review + propose (and optionally promote) |
| `policy` | Show the kid's current decision policy (champion) and any pending challenger |
| `graduate [--revoke]` | Show real-money readiness; the mother approves a live account only when all criteria pass |

---

## Architecture

```
tentacle/
  config.py        all thresholds, ladder rungs, weights, size bands, catalyst specs
  models.py        the data contracts (RawSignal -> ClassifiedSignal -> Candidate -> Trade -> Outcome)
  storage.py       SQLite persistence (signals, candidates, trades, outcomes, ledger, tuned weights, kv)
  marketdata.py    prices/bars/VWAP/volume/clock (Alpaca data API, yfinance fallback)
  sources/         one module per tentacle; auto-discovered by the registry, env-gated
  brain/           classifier, confirmation gate, confidence pre-filter, the Claude decision-maker (decider+llm), judge fallback, pillars
  risk/            regime, caps, settled-cash/GFV, sizing, and the RiskManager LAW
  ladder/          mastery score + promotion/demotion/death
  learning/        outcome tracking, calibration, and the weight/prior tuner
  policy.py        the kid's editable decision policy (system prompt + learned lessons)
  mother/          the meta-learning coach: guardian (Eye 2), blind review (Eye 1), champion/challenger
  broker/          Broker ABC + SimulatorBroker, AlpacaBroker (paper default), Robinhood stub
  backtest/        the Rung -1 gauntlet (real PEAD study + risk Monte Carlo)
  engine/          persistent state + the cycle loop that orchestrates all of the above
```

One `run_cycle()` is the full heartbeat: settle T+1 -> poll tentacles -> classify
-> confirm/group -> score -> judge -> apply the risk law -> fire or shadow ->
manage open positions -> resolve outcomes -> (periodically) learn and evaluate the
ladder -> snapshot the ledger.

---

## What is real vs proxied (honesty notes)

- The **PEAD backtest is real**: point-in-time forward drift after thousands of
  historical earnings dates across a liquid basket. It deliberately measures drift
  after *every* report (no beat filter), a strictly harder bar than the live system
  (which needs a confirmed beat plus multi-family confirmation). It seeds the
  earnings prior from data, and at last run it correctly **refused to promote**
  because the naive proxy does not clear the bar.
- The **risk Monte Carlo is synthetic**: it proves the ruin floor is mathematically
  contained and expectancy is positive *given* a plausible edge. It does not prove
  the edge exists; the live shadow stage (Rung 0) does that with real forward signals.
- A full historical **news** backtest is infeasible for free, so Rung 0 (live
  shadow/paper, $0) is where the real, no-hindsight forward validation happens
  before any money is risked.

---

## Safety defaults

- `TENTACLE_ARMED=false` and Rung 0 ($0) out of the box: it only shadow-logs.
- Long only. No options, no crypto, no shorting. A bearish view would be an inverse
  ETF, which is disabled in v1.
- Every network call is guarded; a flaky tentacle yields nothing rather than
  crashing the loop. Unknown prices/regime FAIL SAFE (they block a fire, never
  allow one).
