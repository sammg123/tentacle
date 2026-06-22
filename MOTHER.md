# The Mother: TENTACLE's meta-learning coach

The family:
- **The kid (the trader):** Claude making BUY/PASS + conviction + a written thesis
  (`brain/decider.py`). He journals every decision.
- **The mother (the coach):** a separate Claude role that reads the kid's journal,
  judges the QUALITY of his thinking, watches that he is not bleeding money, and
  proposes bounded improvements to how he decides. She raises him; she does not
  trade.
- **The household rules:** the ladder + the risk law (`risk/`, `config.py`). Neither
  the kid nor the mother can edit these. They are the floor under everyone.

The mother edits exactly ONE thing: the kid's decision **policy** = his system
prompt + a list of learned "lessons" (`policy.py`). She can NEVER touch sizing,
stops, the ruin floor, the daily halt, the ladder caps, or the settled-cash rule.
Worst case she gives the kid bad advice, and the risk law still contains the damage.

---

## What "performing well" means (the central definition)

You cannot judge the kid on recent P&L, because over small samples P&L is mostly
luck: a great decision can lose, a stupid one can win. Judging on P&L punishes good
process after an unlucky streak (outcome bias) and is how these systems rot. So:

**Performing well = good calibration + coherent, rule-following reasoning + positive
risk-adjusted edge at statistically meaningful sample sizes, judged on the
information available at decision time, never on the raw P&L of a small sample.**

Ranked signals the mother uses:
1. **Calibration (primary, direction-agnostic):** does a conviction-90 call win ~90%
   of comparable horizons? Reuses `learning/calibration.py`.
2. **Reasoning integrity (primary, judged BLIND at decision-time):** coherent, cites
   the real evidence, follows the rules, avoids known traps (chasing, single-echo
   trust, stale timestamps, pumps).
3. **Edge vs benchmark (secondary, slow, stat-gated):** expectancy beats doing
   nothing / SPY and beats the shadow counterfactual, with enough samples that a
   bootstrap CI excludes zero. Never act on a handful of trades.
4. **Discipline / selectivity:** rare high-conviction entries, not overtrading.

The teaching contrast: a losing month with great calibration + sound rationale +
positive expectancy-at-scale is performing WELL (leave him alone). A winning month
from one lucky incoherent YOLO is NOT (fix the process). Reward process, not result.

---

## The mother's two eyes

### Eye 1: blind process review (`mother/review.py`)
Reads a batch of recent journaled decisions with ALL outcome fields STRIPPED (no
win/loss, no P&L, no exit). Scores each on a rubric and aggregates systematic flaws,
then proposes a challenger policy. Uses `brain/llm.claude_complete` (subscription CLI
first), with a deterministic heuristic fallback. It must be impossible for an outcome
to reach this eye.

### Eye 2: financial guardian (`mother/guardian.py`)
Pure computation over the journal + outcomes + closed trades. Sees outcomes, but only
in AGGREGATE, and only alerts on a THRESHOLD CROSSING (state change ok->bad), never per
trade. Cheap; safe to run every cycle. Alerts go to `notify`. It does NOT bypass the
hard law and by default does NOT change behavior (optional `auto_defensive` may disarm).

### Anti-overcorrection + champion/challenger (`mother/champion.py`)
The mother never edits the live kid directly. She writes a **challenger** policy; it is
promoted to **champion** only if (a) a cooldown has elapsed, (b) the champion is NOT
already performing well (do not fix a winner), (c) the challenger clearly beats the
champion by a margin, and (d) meta-calibration has not flagged the mother as unhelpful.
Every change is logged with a metrics snapshot so we can later measure whether it
helped. Who watches the mother: if her promoted changes do not improve metrics, her
trust score drops and she needs a bigger margin to act.

Honesty note: an LLM critiquing an LLM can share blind spots, and process-score
improvement is not proof of live edge. The real validation is forward, in shadow.

IMPLEMENTED (forward shadow A/B, `mother/abtest.py`): when a challenger exists, the
decider also runs it on the SAME finalists each cycle (shadow only, never trades) and
logs its verdict to `ab_decisions`. Because both arms judge identical candidates and
inherit identical outcomes, `forward_ab_compare` measures the mean realized drift each
arm captured, a clean market-direction-controlled edge test. `evaluate_promotion`
prefers this forward A/B once there are >= `min_ab_samples` resolved pairs and only
falls back to the process rubric below that. The heavy review/propose pass is also
gated on >= `min_new_decisions` new decisions since the last review (run_mother), so
the mother never grades noise.

---

## Contracts for the fan-out (match these signatures exactly)

Read first: `MOTHER.md` (this file), `tentacle/policy.py`, `tentacle/storage.py`
(the `decisions` + `strategy_changes` tables and helpers), `tentacle/config.py`
(`MotherConfig`), `tentacle/brain/llm.py` (`claude_complete`, `claude_available`),
`tentacle/learning/calibration.py`. NO em dashes anywhere. Guard all errors. Heavy
deps guarded. Import-test with `.venv/bin/python`.

### mother/guardian.py
```
def assess_health(storage, cfg=None, md=None) -> dict
    # Pure computation. Return:
    # {"stage_start": float, "equity": float, "peak_equity": float,
    #  "drawdown_pct": float, "stage_pnl": float, "loss_streak": int,
    #  "rolling_expectancy": float, "rolling_n": int, "calibration_error": float,
    #  "samples": int, "alerts": [{"code","severity","message"}], "healthy": bool}
    # drawdown vs stage peak from the equity_ledger; loss_streak from closed_trades;
    # rolling_expectancy = mean R over last N closed (R = pnl / risk, risk = entry*stop_pct);
    # calibration_error from learning.calibration.calibration_error.
    # alert codes: DRAWDOWN (drawdown_pct >= cfg.mother.drawdown_alert_pct),
    #   LOSS_STREAK (>= cfg.mother.loss_streak_alert),
    #   NEG_EXPECTANCY (rolling_expectancy < 0 with rolling_n >= cfg.mother.min_edge_samples),
    #   MISCALIBRATED (calibration_error >= cfg.mother.calibration_alert with samples>=min).
    # healthy = no alerts. FAIL SAFE on missing data (treat as healthy=False only for
    #   DRAWDOWN if equity unknown? No: unknown -> no alert, but note it).

def check_and_alert(storage, cfg=None, md=None, notify=None) -> dict
    # Run assess_health, compare each alert code to kv "mother.guardian_state"
    # (the set of codes already active). Notify (call notify(msg) if given) ONLY for
    # codes newly transitioning inactive->active. Clear codes that recovered. Persist
    # the new active set. Return {"assessment":..., "new_alerts":[...], "cleared":[...]}.
    # If cfg.mother.auto_defensive and a new DRAWDOWN alert fires, set kv "armed"=False
    #   and note it (the hard law already handles ruin; this is an early brake).
```

### mother/review.py  (BLIND to outcomes)
```
def review_decisions(storage, cfg=None, limit=None) -> dict
    # Pull storage.recent_decisions(limit or cfg.mother.review_batch, with_outcomes=False).
    # If none -> {"n_reviewed":0,...}. Build a prompt giving ONLY: ticker, catalyst,
    # families, conviction, thesis, evidence headlines, hours_since_event, rules summary.
    # NEVER include win/loss/pnl/exit/label. Ask Claude (claude_complete) as an UNBIASED
    # coach to score each decision 0-100 on: coherence, evidence_grounded, rule_adherence,
    # trap_avoidance, plus verdict_sensible (bool) and a one-line note; and to list
    # systematic flaws across the batch. Return:
    # {"n_reviewed":int, "avg_process_score":float, "systematic_flaws":[str],
    #  "per_decision":[{...}], "blind":True, "backend":str}.
    # Heuristic fallback (no LLM): score from reason flags already on the decision row
    #   (e.g. presence of confirmation-ok reasons, freshness) so it still returns numbers.

def propose_challenger(storage, cfg=None, review=None) -> dict
    # review = review_decisions(...) if None. If avg_process_score is already high
    # (>= cfg.mother.well_process_score) and no systematic_flaws -> propose nothing,
    # return {"proposed":False,"reason":"kid is reasoning well"}.
    # Else ask Claude to propose 1-3 concise imperative "lessons" to append to the
    # kid's policy that address the flaws, plus a one-line changelog. Lessons must be
    # about REASONING only and must never mention sizing/stops/caps. Store via
    # policy.set_challenger(storage, {...}). Return {"proposed":True,"lessons":[...],
    # "changelog":str,"version":int}.
```

### mother/champion.py
```
def evaluate_promotion(storage, cfg=None, md=None) -> dict
    # Decide whether to promote the stored challenger to champion. HOLD if:
    #  - no challenger exists, or
    #  - cooldown not elapsed since last promotion (kv "policy.last_promotion_at",
    #    cfg.mother.promotion_cooldown_hours), or
    #  - the champion is "performing well" (guardian.assess_health healthy AND
    #    calibration_error < cfg.mother.well_calibration AND a recent review
    #    avg_process_score >= cfg.mother.well_process_score with enough samples)
    #    -> do not fix a winner.
    # Otherwise PROMOTE only if the challenger beats the champion by margin: re-run a
    # BLIND review-style scoring of a holdout of recent journaled decisions under the
    # challenger's lessons vs the champion's, require challenger_score >=
    # champion_score + cfg.mother.promotion_margin AND mother_trust allows it.
    # On promote: policy.promote_challenger(storage); record a strategy_changes row with
    # metrics_before; set kv "policy.last_promotion_at". Return
    # {"action":"promote|hold|reject","reason":str,"champion_score":..,"challenger_score":..}.

def meta_calibration(storage, cfg=None, md=None) -> dict
    # For past promotions in strategy_changes lacking metrics_after, if enough time/
    # samples have passed, fill metrics_after (current calibration_error, expectancy,
    # avg_process_score) and judge improved=bool. Maintain kv "mother.trust" in [0.3,1.5]:
    # nudge down when changes did not help, up when they did. Return a summary.
```

### mother/__init__.py
```
def run_mother(storage=None, cfg=None, md=None, notify=None,
               propose=True, promote=False) -> dict
    # Orchestrate one mother pass: guardian.check_and_alert -> review_decisions ->
    # (if propose) propose_challenger -> (if promote) evaluate_promotion ->
    # meta_calibration. Return a single report dict with all sub-results. Never raises.
```

### scripts (runner)
- `run.sh`: load `.env` if present, then `exec .venv/bin/python -m tentacle run`.
- `mother.sh`: load `.env`, then `.venv/bin/python -m tentacle mother --promote`.
- `com.tentacle.run.plist`: a launchd agent that keeps `run.sh` alive (RunAtLoad,
  KeepAlive), logging to `data/tentacle.out`/`data/tentacle.err`. Include a short
  comment block explaining `launchctl load` and the weekly `mother.sh` via cron/calendar.
```
