"""Judge: the final, bounded sanity pass over the top candidates.

Responsibilities (CONTRACTS.md brain/judge):
  * Sort by confidence desc, take top_n (default cfg.engine.max_candidates_to_judge).
  * Require >= 2 ORTHOGONAL evidence TYPES. A "type" is a distinct signal family
    OR a non-neutral pillar (technical / options / fundamentals). If a candidate
    has < 2 orthogonal types, append a reason and set decision to "shadow"
    (it will not fire).
  * Run the pillars, fold them into cand.pillar_scores. A technical VETO appends a
    "technical_veto" reason and knocks confidence down hard (* 0.5).
  * If cfg.integrations.anthropic_key is set, ask Claude (model "claude-sonnet-4-6")
    as a final analyst over these few candidates, via the Anthropic Messages API
    over `requests` (bounded, small max_tokens, JSON-ish keep/cut + one-line reason).
    On ANY error fall back to the heuristic. No key -> heuristic only.
  * Return the (possibly re-scored) candidates. NEVER places an order.

Everything fails safe: an unscored/erroring candidate ends up "shadow", never
"fire". The judge only lowers conviction or flags; the risk module owns the law.
"""
from __future__ import annotations

import json
from typing import Optional

from ..config import get_config
from ..models import Candidate, DecisionType
from ..util import get_logger
from .pillars import fundamentals as _fundamentals
from .pillars import options_flow as _options_flow
from .pillars import technical as _technical

log = get_logger("tentacle.brain.judge")

_CLAUDE_MODEL = "claude-sonnet-4-6"
_CLAUDE_URL = "https://api.anthropic.com/v1/messages"
_CLAUDE_MAX_TOKENS = 400
_CLAUDE_TIMEOUT = 20.0
# A non-neutral pillar counts as an orthogonal evidence type at/above this size.
_PILLAR_ACTIVE = 0.15


def judge_candidates(cands: list[Candidate], cfg=None, top_n=None,
                     md=None, storage=None) -> list[Candidate]:
    """Judge and re-score the top candidates. Never raises; returns a list."""
    if cfg is None:
        cfg = get_config()
    try:
        if not cands:
            return []
        if top_n is None:
            top_n = cfg.engine.max_candidates_to_judge

        ordered = sorted(cands, key=lambda c: _safe_conf(c), reverse=True)
        head = ordered[: max(0, int(top_n))]

        # Only the top_n get the expensive deep judge (pillars + Claude). They are
        # the only fire-eligible candidates; _judge_one stamps pillar_scores, which
        # the engine uses as the "was deep-judged" marker before allowing a fire.
        for cand in head:
            _judge_one(cand, cfg=cfg, md=md, storage=storage)

        # Optional Claude analyst over the (already pillar-adjusted) head.
        key = ""
        try:
            key = cfg.integrations.anthropic_key or ""
        except Exception:
            key = ""
        if key:
            try:
                _claude_analyst(head, key, cfg)
            except Exception as exc:  # noqa: BLE001 never let the analyst crash judging
                log.debug("claude analyst failed, heuristic only: %s", exc)

        # Return ALL candidates (head deep-judged in place, tail untouched) so the
        # engine can shadow-log every confirmation-passer for learning. Only the
        # deep-judged head is fire-eligible.
        return ordered
    except Exception as exc:  # noqa: BLE001
        log.debug("judge_candidates error: %s", exc)
        return cands or []


def _judge_one(cand: Candidate, cfg, md, storage) -> None:
    """Run pillars + the orthogonal-evidence rule for one candidate, in place."""
    try:
        # --- Run the three pillars (each neutral-by-default, never raises) ---
        tech = _technical.technical_read(cand.ticker, cfg=cfg, md=md)
        opts = _options_flow.options_flow_read(cand.ticker, cfg=cfg)
        fund = _fundamentals.fundamentals_read(cand.ticker, cfg=cfg, md=md)

        pillars = dict(cand.pillar_scores) if cand.pillar_scores else {}
        pillars["technical"] = tech
        pillars["options_flow"] = opts
        pillars["fundamentals"] = fund
        cand.pillar_scores = pillars

        # --- Fold a fundamentals squeeze boost into confidence (bounded) ---
        squeeze = _num(fund.get("squeeze_boost"), 0.0)
        if squeeze > 0:
            cand.confidence = _clamp(cand.confidence * (1.0 + squeeze), 0.0, 100.0)
            cand.add_reason(f"squeeze_boost +{squeeze:.2f}")

        # --- Count orthogonal evidence TYPES: distinct families + active pillars ---
        family_types = set(cand.families)
        pillar_types: set[str] = set()
        if abs(_num(tech.get("score"), 0.0)) >= _PILLAR_ACTIVE or tech.get("veto"):
            pillar_types.add("pillar:technical")
        if abs(_num(opts.get("score"), 0.0)) >= _PILLAR_ACTIVE:
            pillar_types.add("pillar:options_flow")
        if abs(_num(fund.get("score"), 0.0)) >= _PILLAR_ACTIVE or squeeze > 0:
            pillar_types.add("pillar:fundamentals")
        n_types = len(family_types) + len(pillar_types)

        if n_types < 2:
            cand.decision = DecisionType.SHADOW.value
            cand.add_reason(
                f"insufficient orthogonal evidence: {n_types} type(s) "
                f"(families={sorted(family_types)}, pillars={sorted(pillar_types)})"
            )

        # --- Technical veto: cut conviction hard ---
        if tech.get("veto"):
            before = cand.confidence
            cand.confidence = _clamp(cand.confidence * 0.5, 0.0, 100.0)
            cand.add_reason(
                f"technical_veto: {tech.get('note', 'bearish reversal at highs')} "
                f"(confidence {before:.0f} -> {cand.confidence:.0f})"
            )

        # Negative technical score (extended/weak tape) is a softer drag.
        tscore = _num(tech.get("score"), 0.0)
        if tscore < 0 and not tech.get("veto"):
            cand.confidence = _clamp(cand.confidence * (1.0 + 0.15 * tscore), 0.0, 100.0)

        # Negative fundamentals (cash burn) trims a little conviction.
        fscore = _num(fund.get("score"), 0.0)
        if fscore < 0:
            cand.confidence = _clamp(cand.confidence * (1.0 + 0.10 * fscore), 0.0, 100.0)

        # Keep size_fraction consistent with the (possibly adjusted) confidence.
        try:
            from ..config import size_fraction_for
            cand.size_fraction = float(size_fraction_for(cand.confidence))
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        log.debug("pillar judging failed for %s: %s", getattr(cand, "ticker", "?"), exc)
        # Fail safe: if we cannot judge it, do not let it look fire-ready.
        cand.decision = DecisionType.SHADOW.value
        cand.add_reason("judge error -> shadow (fail safe)")


# --------------------------------------------------------------------------- #
# Optional Claude analyst (bounded; raw HTTP via requests; model claude-sonnet-4-6)
# --------------------------------------------------------------------------- #
def _claude_analyst(cands: list[Candidate], api_key: str, cfg) -> None:
    """Ask Claude for keep/cut + one-line reason per candidate. Best-effort.

    Uses the Anthropic Messages API directly over `requests` (no SDK dependency in
    the core), model "claude-sonnet-4-6", a small max_tokens, and a JSON-ish
    response. Sets cand.judge_notes. On any error the caller falls back to the
    heuristic result already computed by _judge_one. A "cut" verdict downgrades
    the decision to shadow; the analyst can never UPGRADE a candidate to fire.
    """
    if not cands:
        return
    try:
        import requests  # imported here so the module loads without it
    except Exception:
        return

    payload_items = []
    for i, c in enumerate(cands):
        tech = (c.pillar_scores or {}).get("technical", {}) if c.pillar_scores else {}
        fund = (c.pillar_scores or {}).get("fundamentals", {}) if c.pillar_scores else {}
        titles = []
        for cs in c.signals[:4]:
            raw = getattr(cs, "raw", None)
            if raw is not None and raw.title:
                titles.append(raw.title[:160])
        payload_items.append({
            "id": i,
            "ticker": c.ticker,
            "catalyst": c.catalyst,
            "confidence": round(_num(c.confidence, 0.0), 1),
            "families": sorted(c.families),
            "technical_note": str(tech.get("note", ""))[:160],
            "technical_veto": bool(tech.get("veto", False)),
            "fundamentals_note": str(fund.get("note", ""))[:160],
            "headlines": titles,
        })

    system = (
        "You are a disciplined long-only equities risk analyst. For each candidate "
        "trade (a ticker + a news catalyst with supporting evidence), decide keep "
        "or cut. KEEP only if the catalyst is real, specific, and not a pump or a "
        "stale rehash, and there are at least two independent kinds of evidence. "
        "Be skeptical: when unsure, CUT. You only filter; you never increase "
        "conviction. Respond ONLY with a JSON array, one object per candidate: "
        '{"id": <int>, "verdict": "keep"|"cut", "reason": "<one short line>"}.'
    )
    user = (
        "Candidates:\n" + json.dumps(payload_items, ensure_ascii=False) +
        "\n\nReturn the JSON array now."
    )

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": _CLAUDE_MODEL,
        "max_tokens": _CLAUDE_MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }

    try:
        resp = requests.post(_CLAUDE_URL, headers=headers,
                             data=json.dumps(body), timeout=_CLAUDE_TIMEOUT)
    except Exception:
        return
    if resp is None or resp.status_code != 200:
        return

    text = _extract_text(resp)
    if not text:
        return
    verdicts = _parse_verdicts(text)
    if not verdicts:
        return

    for c in cands:
        idx = cands.index(c)
        v = verdicts.get(idx)
        if not v:
            continue
        verdict = str(v.get("verdict", "")).lower()
        reason = str(v.get("reason", ""))[:200]
        c.judge_notes = f"claude:{verdict}: {reason}".strip()
        # The analyst can only filter DOWN, never fire something up.
        if verdict == "cut":
            c.decision = DecisionType.SHADOW.value
            c.add_reason(f"claude analyst cut: {reason}")


def _extract_text(resp) -> str:
    """Pull the assistant text out of a Messages API response. Empty on failure."""
    try:
        data = resp.json()
    except Exception:
        return ""
    try:
        blocks = data.get("content") or []
        parts = [b.get("text", "") for b in blocks
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts).strip()
    except Exception:
        return ""


def _parse_verdicts(text: str) -> dict[int, dict]:
    """Parse the model's JSON array into {id: {verdict, reason}}. Tolerant."""
    out: dict[int, dict] = {}
    raw = _slice_json_array(text)
    if not raw:
        return out
    try:
        arr = json.loads(raw)
    except Exception:
        return out
    if not isinstance(arr, list):
        return out
    for item in arr:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        out[idx] = {
            "verdict": item.get("verdict", ""),
            "reason": item.get("reason", ""),
        }
    return out


def _slice_json_array(text: str) -> str:
    """Best-effort extraction of the first [...] JSON array from free text."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return ""
    return text[start:end + 1]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _safe_conf(c: Candidate) -> float:
    return _num(getattr(c, "confidence", 0.0), 0.0)


def _num(v, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return f
