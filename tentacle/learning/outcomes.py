"""Outcome logging and resolution.

The learning loop only works if we record what WOULD have happened, not just what
we traded. So every candidate gets a pending Outcome: fired trades (fired=True) and
shadow signals (fired=False) alike. That way the bot learns from the road not taken
and we can calibrate confidence against reality without survivorship bias.

A pending outcome resolves once the catalyst's hold horizon has elapsed: we fetch
the current price, compute the realized drift, and label it win, loss, or flat.
Network and parse errors are swallowed so a flaky price feed never crashes the loop.
"""
from __future__ import annotations

from typing import Optional

from ..config import Config, catalyst_spec, get_config
from ..models import Candidate, Outcome, OutcomeLabel
from ..util import age_seconds, get_logger, now_iso

log = get_logger("tentacle.learning.outcomes")

# A move within +-1% over the horizon is treated as "flat" (no real edge either way).
_FLAT_BAND = 0.01


def log_outcome(
    cand: Candidate,
    entry_price: float,
    fired: bool,
    storage,
    cfg: Optional[Config] = None,
) -> str:
    """Create and persist a PENDING Outcome for a candidate.

    Records both fired trades and shadow signals so the learning loop sees the full
    population of decisions. Returns the outcome id, or an empty string if it could
    not be persisted (we never raise out of the learning path).
    """
    cfg = cfg or get_config()
    try:
        catalyst = getattr(cand, "catalyst", "") or ""
        horizon = catalyst_spec(catalyst).horizon_hours

        # candidate_id mirrors storage.save_candidate's stable id so outcomes and
        # candidates can be joined later. Fall back to a per-ticker id if the helper
        # is unavailable for any reason.
        try:
            from ..util import stable_id
            cid = stable_id(
                cand.ticker,
                catalyst,
                cand.first_event_time or "",
                ",".join(sorted(cand.source_ids)),
            )
        except Exception:
            cid = ""

        try:
            entry = float(entry_price)
        except (TypeError, ValueError):
            entry = 0.0

        outcome = Outcome(
            candidate_id=cid,
            ticker=cand.ticker,
            catalyst=catalyst,
            confidence=float(getattr(cand, "confidence", 0.0) or 0.0),
            fired=bool(fired),
            entry_price=entry,
            horizon_hours=float(horizon),
            source_ids=sorted(cand.source_ids),
            families=sorted(cand.families),
            label=OutcomeLabel.PENDING.value,
        )
        storage.save_outcome(outcome)
        return outcome.id
    except Exception as exc:  # noqa: BLE001
        log.debug("log_outcome failed for %s: %s", getattr(cand, "ticker", "?"), exc)
        return ""


def resolve_due_outcomes(storage, md=None, cfg: Optional[Config] = None) -> int:
    """Resolve every pending outcome whose hold horizon has elapsed.

    For each due outcome: fetch the current price, compute drift_pct relative to the
    entry, label win / loss / flat, and save. Returns the number resolved. Outcomes
    we cannot price yet (price unknown, or horizon not reached) are left pending and
    retried on a later pass. Errors are swallowed per outcome so one bad row never
    blocks the rest.
    """
    cfg = cfg or get_config()
    if md is None:
        try:
            from ..marketdata import get_marketdata
            md = get_marketdata()
        except Exception:  # noqa: BLE001
            md = None

    resolved = 0
    try:
        pending = storage.pending_outcomes()
    except Exception as exc:  # noqa: BLE001
        log.debug("pending_outcomes failed: %s", exc)
        return 0

    for row in pending:
        try:
            created_at = row.get("created_at")
            horizon_hours = float(row.get("horizon_hours") or 0.0)
            # horizon not elapsed yet -> leave pending.
            elapsed_h = age_seconds(created_at) / 3600.0
            if elapsed_h < horizon_hours:
                continue

            ticker = row.get("ticker")
            entry = row.get("entry_price")
            try:
                entry = float(entry)
            except (TypeError, ValueError):
                entry = 0.0
            if not ticker or entry <= 0.0:
                # Cannot compute a meaningful drift; skip (FAIL SAFE, stays pending).
                continue

            price = None
            if md is not None:
                try:
                    price = md.last_price(ticker)
                except Exception:  # noqa: BLE001
                    price = None
            if price is None:
                # Price unknown right now: do NOT resolve, retry next pass.
                continue
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            if price <= 0.0:
                continue

            drift = (price - entry) / entry
            if drift > _FLAT_BAND:
                label = OutcomeLabel.WIN.value
            elif drift < -_FLAT_BAND:
                label = OutcomeLabel.LOSS.value
            else:
                label = OutcomeLabel.FLAT.value

            outcome = Outcome(
                candidate_id=row.get("candidate_id") or "",
                ticker=ticker,
                catalyst=row.get("catalyst") or "",
                confidence=float(row.get("confidence") or 0.0),
                fired=bool(row.get("fired")),
                entry_price=entry,
                horizon_hours=horizon_hours,
                exit_price=price,
                drift_pct=drift,
                label=label,
                source_ids=_loads_list(row.get("source_ids")),
                families=_loads_list(row.get("families")),
                resolved_at=now_iso(),
                created_at=created_at,
                id=row.get("id") or "",
            )
            storage.save_outcome(outcome)
            resolved += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("resolve outcome %s failed: %s", row.get("id"), exc)
            continue

    return resolved


def _loads_list(value) -> list:
    """Best-effort decode of a stored JSON list column back into a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        import json
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
    except Exception:  # noqa: BLE001
        pass
    return []
