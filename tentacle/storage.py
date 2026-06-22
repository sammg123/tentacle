"""SQLite persistence. stdlib only, no external dependency.

One Storage object wraps a single connection (thread-safe via a lock) and exposes
typed helpers. Everything the learning loop and the ladder need to survive a
restart lives here: signals, candidates, trades, outcomes, the equity ledger,
per-source tuned weights, per-catalyst priors, and a key/value engine-state store.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import asdict
from typing import Any, Optional

from .models import Outcome, RawSignal, Trade
from .util import get_logger, now_iso

log = get_logger("tentacle.storage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_signals (
    dedup_key       TEXT PRIMARY KEY,
    source_id       TEXT,
    family          TEXT,
    tier            INTEGER,
    title           TEXT,
    url             TEXT,
    summary         TEXT,
    tickers         TEXT,
    event_time      TEXT,
    fetched_at      TEXT,
    base_reliability REAL,
    signal_strength REAL,
    catalyst_hint   TEXT,
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_fetched ON raw_signals(fetched_at);
CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_signals(source_id);

CREATE TABLE IF NOT EXISTS source_state (
    source_id   TEXT PRIMARY KEY,
    cursor      TEXT,            -- json: last-seen ids / timestamps
    tuned_weight REAL,          -- learning-tuned reliability multiplier (1.0 = neutral)
    fired       INTEGER DEFAULT 0,
    wins        INTEGER DEFAULT 0,
    losses      INTEGER DEFAULT 0,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS catalyst_priors (
    catalyst    TEXT PRIMARY KEY,
    drift_prior REAL,
    samples     INTEGER DEFAULT 0,
    wins        INTEGER DEFAULT 0,
    sum_drift   REAL DEFAULT 0,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
    id          TEXT PRIMARY KEY,
    ticker      TEXT,
    catalyst    TEXT,
    confidence  REAL,
    size_fraction REAL,
    size_dollars  REAL,
    decision    TEXT,
    families    TEXT,
    source_ids  TEXT,
    reasons     TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_cand_created ON candidates(created_at);

CREATE TABLE IF NOT EXISTS trades (
    id          TEXT PRIMARY KEY,
    ticker      TEXT,
    qty         REAL,
    entry_price REAL,
    size_dollars REAL,
    confidence  REAL,
    catalyst    TEXT,
    rung        INTEGER,
    horizon_hours REAL,
    stop_price  REAL,
    target_price REAL,
    entry_time  TEXT,
    paper       INTEGER,
    status      TEXT,
    exit_price  REAL,
    exit_time   TEXT,
    pnl         REAL,
    outcome     TEXT,
    candidate_id TEXT,
    broker_order_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);

CREATE TABLE IF NOT EXISTS outcomes (
    id          TEXT PRIMARY KEY,
    candidate_id TEXT,
    ticker      TEXT,
    catalyst    TEXT,
    confidence  REAL,
    fired       INTEGER,
    entry_price REAL,
    horizon_hours REAL,
    exit_price  REAL,
    drift_pct   REAL,
    label       TEXT,
    source_ids  TEXT,
    families    TEXT,
    resolved_at TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_out_label ON outcomes(label);
CREATE INDEX IF NOT EXISTS idx_out_catalyst ON outcomes(catalyst);

CREATE TABLE IF NOT EXISTS equity_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT,
    rung        INTEGER,
    equity      REAL,
    cash        REAL,
    settled     REAL,
    unsettled   REAL,
    realized_pnl REAL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS mastery_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT,
    rung        INTEGER,
    mastery     REAL,
    components  TEXT
);

CREATE TABLE IF NOT EXISTS settlements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    amount      REAL,
    settles_on  TEXT,             -- ISO date when funds become settled
    settled     INTEGER DEFAULT 0,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

-- The kid's decision journal: the full record the mother reviews. Outcomes are
-- NOT stored here; they live in `outcomes` and are joined on candidate_id only for
-- the guardian. The blind process review reads this table WITHOUT the join.
CREATE TABLE IF NOT EXISTS decisions (
    candidate_id    TEXT PRIMARY KEY,
    ticker          TEXT,
    catalyst        TEXT,
    verdict         TEXT,          -- buy | pass
    conviction      REAL,
    thesis          TEXT,
    evidence        TEXT,          -- json: list of {family, src, title}
    families        TEXT,          -- json
    rules           TEXT,          -- json snapshot of the gate/risk knobs in force
    backend         TEXT,          -- cli | api | heuristic
    fired           INTEGER,
    entry_price     REAL,
    rung            INTEGER,
    policy_version  INTEGER,
    decided_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_dec_decided ON decisions(decided_at);

-- The challenger arm of the forward shadow A/B. The champion's decision lives in
-- `decisions`; this is the challenger's parallel call on the SAME candidate (shadow
-- only, never trades). Joined to `decisions` and `outcomes` on candidate_id, both
-- arms saw identical candidates and identical outcomes, so the only difference is
-- the decision: a fair, market-direction-controlled edge comparison.
CREATE TABLE IF NOT EXISTS ab_decisions (
    candidate_id    TEXT PRIMARY KEY,
    verdict         TEXT,          -- buy | pass
    conviction      REAL,
    policy_version  INTEGER,
    decided_at      TEXT
);

-- The mother's change log, for meta-calibration ("did her changes help?").
CREATE TABLE IF NOT EXISTS strategy_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version         INTEGER,
    kind            TEXT,          -- challenger | promote
    summary         TEXT,
    metrics_before  TEXT,          -- json
    metrics_after   TEXT,          -- json (filled later by meta_calibration)
    improved        INTEGER,       -- 1/0/null
    created_at      TEXT,
    measured_at     TEXT
);
"""


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)


class Storage:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---------------------------------------------------------------- raw signals
    def signal_seen(self, dedup_key: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM raw_signals WHERE dedup_key=? LIMIT 1", (dedup_key,)
            )
            return cur.fetchone() is not None

    def save_signal(self, sig: RawSignal) -> bool:
        """Insert a signal. Returns True if NEW, False if already seen."""
        with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO raw_signals
                    (dedup_key, source_id, family, tier, title, url, summary,
                     tickers, event_time, fetched_at, base_reliability,
                     signal_strength, catalyst_hint, raw)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        sig.dedup_key, sig.source_id, sig.family, sig.tier,
                        sig.title, sig.url, sig.summary, _dumps(sig.tickers),
                        sig.event_time, sig.fetched_at, sig.base_reliability,
                        sig.signal_strength, sig.catalyst_hint, _dumps(sig.raw),
                    ),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def recent_signals(self, since_iso: str) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM raw_signals WHERE fetched_at >= ? ORDER BY fetched_at",
                (since_iso,),
            )
            return [dict(r) for r in cur.fetchall()]

    # ---------------------------------------------------------------- source state
    def get_source_cursor(self, source_id: str) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "SELECT cursor FROM source_state WHERE source_id=?", (source_id,)
            )
            row = cur.fetchone()
            if row and row["cursor"]:
                try:
                    return json.loads(row["cursor"])
                except Exception:
                    return {}
            return {}

    def set_source_cursor(self, source_id: str, cursor: dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO source_state (source_id, cursor, tuned_weight, updated_at)
                   VALUES (?,?,COALESCE((SELECT tuned_weight FROM source_state WHERE source_id=?),1.0),?)
                   ON CONFLICT(source_id) DO UPDATE SET cursor=excluded.cursor,
                       updated_at=excluded.updated_at""",
                (source_id, _dumps(cursor), source_id, now_iso()),
            )
            self._conn.commit()

    def get_source_weight(self, source_id: str) -> float:
        with self._lock:
            cur = self._conn.execute(
                "SELECT tuned_weight FROM source_state WHERE source_id=?", (source_id,)
            )
            row = cur.fetchone()
            return float(row["tuned_weight"]) if row and row["tuned_weight"] is not None else 1.0

    def set_source_weight(self, source_id: str, weight: float,
                          fired: int = 0, wins: int = 0, losses: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO source_state
                   (source_id, tuned_weight, fired, wins, losses, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET tuned_weight=excluded.tuned_weight,
                     fired=source_state.fired+excluded.fired,
                     wins=source_state.wins+excluded.wins,
                     losses=source_state.losses+excluded.losses,
                     updated_at=excluded.updated_at""",
                (source_id, weight, fired, wins, losses, now_iso()),
            )
            self._conn.commit()

    def all_source_weights(self) -> dict[str, float]:
        with self._lock:
            cur = self._conn.execute("SELECT source_id, tuned_weight FROM source_state")
            return {r["source_id"]: (r["tuned_weight"] or 1.0) for r in cur.fetchall()}

    # ---------------------------------------------------------------- catalyst priors
    def get_catalyst_prior(self, catalyst: str) -> Optional[float]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT drift_prior FROM catalyst_priors WHERE catalyst=?", (catalyst,)
            )
            row = cur.fetchone()
            return float(row["drift_prior"]) if row and row["drift_prior"] is not None else None

    def upsert_catalyst_prior(self, catalyst: str, drift_prior: float,
                              samples: int = 0, wins: int = 0, sum_drift: float = 0.0) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO catalyst_priors
                   (catalyst, drift_prior, samples, wins, sum_drift, updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(catalyst) DO UPDATE SET drift_prior=excluded.drift_prior,
                     samples=catalyst_priors.samples+excluded.samples,
                     wins=catalyst_priors.wins+excluded.wins,
                     sum_drift=catalyst_priors.sum_drift+excluded.sum_drift,
                     updated_at=excluded.updated_at""",
                (catalyst, drift_prior, samples, wins, sum_drift, now_iso()),
            )
            self._conn.commit()

    # ---------------------------------------------------------------- candidates
    def save_candidate(self, cand) -> None:
        from .util import stable_id
        cid = stable_id(cand.ticker, cand.catalyst, cand.first_event_time or "",
                        ",".join(sorted(cand.source_ids)))
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO candidates
                   (id, ticker, catalyst, confidence, size_fraction, size_dollars,
                    decision, families, source_ids, reasons, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, cand.ticker, cand.catalyst, cand.confidence, cand.size_fraction,
                 cand.size_dollars, cand.decision, _dumps(sorted(cand.families)),
                 _dumps(sorted(cand.source_ids)), _dumps(cand.reasons), now_iso()),
            )
            self._conn.commit()
        return cid

    # ---------------------------------------------------------------- trades
    def save_trade(self, trade: Trade) -> None:
        d = asdict(trade)
        d["paper"] = 1 if trade.paper else 0
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO trades
                   (id, ticker, qty, entry_price, size_dollars, confidence, catalyst,
                    rung, horizon_hours, stop_price, target_price, entry_time, paper,
                    status, exit_price, exit_time, pnl, outcome, candidate_id, broker_order_id)
                   VALUES (:id,:ticker,:qty,:entry_price,:size_dollars,:confidence,
                    :catalyst,:rung,:horizon_hours,:stop_price,:target_price,:entry_time,
                    :paper,:status,:exit_price,:exit_time,:pnl,:outcome,:candidate_id,
                    :broker_order_id)""",
                d,
            )
            self._conn.commit()

    def open_trades(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM trades WHERE status='open'")
            return [dict(r) for r in cur.fetchall()]

    def all_trades(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM trades ORDER BY entry_time")
            return [dict(r) for r in cur.fetchall()]

    def closed_trades(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM trades WHERE status='closed' ORDER BY exit_time")
            return [dict(r) for r in cur.fetchall()]

    def has_open_position(self, ticker: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM trades WHERE ticker=? AND status='open' LIMIT 1", (ticker,))
            return cur.fetchone() is not None

    # ---------------------------------------------------------------- outcomes
    def save_outcome(self, outcome: Outcome) -> None:
        d = asdict(outcome)
        d["fired"] = 1 if outcome.fired else 0
        d["source_ids"] = _dumps(outcome.source_ids)
        d["families"] = _dumps(outcome.families)
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO outcomes
                   (id, candidate_id, ticker, catalyst, confidence, fired, entry_price,
                    horizon_hours, exit_price, drift_pct, label, source_ids, families,
                    resolved_at, created_at)
                   VALUES (:id,:candidate_id,:ticker,:catalyst,:confidence,:fired,
                    :entry_price,:horizon_hours,:exit_price,:drift_pct,:label,
                    :source_ids,:families,:resolved_at,:created_at)""",
                d,
            )
            self._conn.commit()

    def pending_outcomes(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM outcomes WHERE label='pending'")
            return [dict(r) for r in cur.fetchall()]

    def resolved_outcomes(self, catalyst: Optional[str] = None) -> list[dict]:
        with self._lock:
            if catalyst:
                cur = self._conn.execute(
                    "SELECT * FROM outcomes WHERE label!='pending' AND catalyst=?",
                    (catalyst,))
            else:
                cur = self._conn.execute(
                    "SELECT * FROM outcomes WHERE label!='pending'")
            return [dict(r) for r in cur.fetchall()]

    # ---------------------------------------------------------------- ledger
    def append_ledger(self, rung: int, equity: float, cash: float, settled: float,
                      unsettled: float, realized_pnl: float, note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO equity_ledger
                   (ts, rung, equity, cash, settled, unsettled, realized_pnl, note)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (now_iso(), rung, equity, cash, settled, unsettled, realized_pnl, note),
            )
            self._conn.commit()

    def equity_curve(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM equity_ledger ORDER BY id")
            return [dict(r) for r in cur.fetchall()]

    # ---------------------------------------------------------------- settlements (T+1)
    def add_settlement(self, amount: float, settles_on: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settlements (amount, settles_on, settled, created_at) VALUES (?,?,0,?)",
                (amount, settles_on, now_iso()),
            )
            self._conn.commit()

    def settle_due(self, today_iso: str) -> float:
        """Mark settlements due on/before today as settled; return total settled."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, amount FROM settlements WHERE settled=0 AND settles_on<=?",
                (today_iso,))
            rows = cur.fetchall()
            total = sum(r["amount"] for r in rows)
            for r in rows:
                self._conn.execute("UPDATE settlements SET settled=1 WHERE id=?", (r["id"],))
            self._conn.commit()
            return total

    def unsettled_total(self) -> float:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS t FROM settlements WHERE settled=0")
            return float(cur.fetchone()["t"])

    # ---------------------------------------------------------------- mastery
    def record_mastery(self, rung: int, mastery: float, components: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO mastery_history (ts, rung, mastery, components) VALUES (?,?,?,?)",
                (now_iso(), rung, mastery, _dumps(components)),
            )
            self._conn.commit()

    def mastery_history(self, limit: int = 50) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM mastery_history ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]

    # ---------------------------------------------------------------- decision journal
    def save_decision(self, *, candidate_id: str, ticker: str, catalyst: str,
                      verdict: str, conviction: float, thesis: str, evidence: list,
                      families: list, rules: dict, backend: str, fired: bool,
                      entry_price: Optional[float], rung: int, policy_version: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO decisions
                   (candidate_id, ticker, catalyst, verdict, conviction, thesis,
                    evidence, families, rules, backend, fired, entry_price, rung,
                    policy_version, decided_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id, ticker, catalyst, verdict, conviction, thesis,
                 _dumps(evidence), _dumps(families), _dumps(rules), backend,
                 1 if fired else 0, entry_price, rung, policy_version, now_iso()),
            )
            self._conn.commit()

    def count_decisions(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) AS n FROM decisions")
            return int(cur.fetchone()["n"])

    def recent_decisions(self, limit: int = 20, with_outcomes: bool = False) -> list[dict]:
        """Most-recent decisions. with_outcomes=False is the BLIND view the mother's
        process review must use (no win/loss reaches it)."""
        with self._lock:
            if not with_outcomes:
                cur = self._conn.execute(
                    "SELECT * FROM decisions ORDER BY decided_at DESC LIMIT ?", (limit,))
                return [dict(r) for r in cur.fetchall()]
            cur = self._conn.execute(
                """SELECT d.*, o.label AS outcome_label, o.drift_pct AS outcome_drift,
                          o.exit_price AS outcome_exit
                   FROM decisions d LEFT JOIN outcomes o ON o.candidate_id = d.candidate_id
                   ORDER BY d.decided_at DESC LIMIT ?""", (limit,))
            return [dict(r) for r in cur.fetchall()]

    # ---------------------------------------------------------------- A/B (challenger arm)
    def save_ab_decision(self, *, candidate_id: str, verdict: str, conviction: float,
                         policy_version: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO ab_decisions
                   (candidate_id, verdict, conviction, policy_version, decided_at)
                   VALUES (?,?,?,?,?)""",
                (candidate_id, verdict, conviction, policy_version, now_iso()),
            )
            self._conn.commit()

    def ab_pairs(self) -> list[dict]:
        """Resolved A/B pairs: champion verdict (decisions) + challenger verdict
        (ab_decisions) + the shared outcome (outcomes), joined on candidate_id.
        Only rows with a resolved, non-flat-unknown drift are returned."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT d.candidate_id        AS candidate_id,
                          d.verdict             AS champ_verdict,
                          a.verdict             AS chall_verdict,
                          d.conviction          AS champ_conviction,
                          a.conviction          AS chall_conviction,
                          o.drift_pct           AS drift_pct,
                          o.label               AS label
                   FROM decisions d
                   JOIN ab_decisions a ON a.candidate_id = d.candidate_id
                   JOIN outcomes o     ON o.candidate_id = d.candidate_id
                   WHERE o.label != 'pending' AND o.drift_pct IS NOT NULL""")
            return [dict(r) for r in cur.fetchall()]

    # ---------------------------------------------------------------- strategy changes
    def save_strategy_change(self, *, version: int, kind: str, summary: str,
                             metrics_before: dict) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO strategy_changes
                   (version, kind, summary, metrics_before, created_at)
                   VALUES (?,?,?,?,?)""",
                (version, kind, summary, _dumps(metrics_before), now_iso()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def unmeasured_changes(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM strategy_changes WHERE metrics_after IS NULL ORDER BY id")
            return [dict(r) for r in cur.fetchall()]

    def finalize_change(self, change_id: int, metrics_after: dict, improved: bool) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE strategy_changes SET metrics_after=?, improved=?, measured_at=?
                   WHERE id=?""",
                (_dumps(metrics_after), 1 if improved else 0, now_iso(), change_id),
            )
            self._conn.commit()

    # ---------------------------------------------------------------- kv (engine state)
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            cur = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,))
            row = cur.fetchone()
            if not row:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return row["value"]

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
                (key, _dumps(value)),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_STORAGE: Optional[Storage] = None


def get_storage(path: Optional[str] = None) -> Storage:
    global _STORAGE
    if _STORAGE is None:
        from .config import get_config
        _STORAGE = Storage(path or get_config().engine.db_path)
    return _STORAGE
