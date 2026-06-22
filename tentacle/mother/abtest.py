"""Forward shadow A/B: run the challenger policy alongside the champion on the SAME
live candidates, log its decisions (shadow only, never trades), and later compare the
two arms on REAL forward outcomes.

This is the rigorous edge test the rubric cannot give. Because both arms judge the
identical candidate and inherit the identical outcome, the only difference is the
decision (buy vs pass), so market direction cancels out: it is a clean measure of
which policy's decisions captured more realized drift.

Note: this is only meaningful with an LLM backend. With the deterministic heuristic,
the challenger's lessons cannot change its verdict, so we record nothing and the
promotion logic falls back to the process-score rubric.
"""
from __future__ import annotations

import json
from typing import Optional

from ..config import get_config
from ..util import get_logger

log = get_logger("tentacle.mother.abtest")


def shadow_challenger_decision(finalists, cfg=None, md=None, storage=None,
                               account=None) -> int:
    """If a challenger policy exists, run it on the same finalists and log its
    verdicts to ab_decisions. Shadow only. Returns the number recorded."""
    cfg = cfg or get_config()
    if not finalists:
        return 0
    try:
        from ..policy import get_challenger_policy, effective_system_prompt
        from ..brain.llm import claude_available, claude_complete
        from ..brain.decider import _SYSTEM, _candidate_view, _account_view, _parse, _clamp, _num
    except Exception as exc:  # noqa: BLE001
        log.debug("abtest imports failed: %s", exc)
        return 0

    if storage is None:
        from ..storage import get_storage
        storage = get_storage()

    challenger = get_challenger_policy(storage)
    if not isinstance(challenger, dict):
        return 0  # no challenger, nothing to A/B
    if claude_available(cfg) == "none":
        return 0  # heuristic mode: lessons cannot change the verdict, A/B is moot

    try:
        payload = [_candidate_view(c, i, md) for i, c in enumerate(finalists)]
        user = (
            f"Account: {_account_view(account, cfg)}\n\n"
            f"Candidates (already confirmation-filtered):\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n\n"
            "Return the JSON array now."
        )
        system = effective_system_prompt(_SYSTEM, policy=challenger)
        text = claude_complete(system, user, cfg, model=cfg.integrations.kid_model)
        verdicts = _parse(text) if text else {}
        if not verdicts:
            return 0
        version = int(challenger.get("version", 0) or 0)
        n = 0
        for i, c in enumerate(finalists):
            v = verdicts.get(i)
            if not v:
                continue
            decision = "buy" if str(v.get("decision", "")).lower() == "buy" else "pass"
            conviction = _clamp(_num(v.get("conviction"), 0.0), 0.0, 100.0)
            if decision == "buy" and conviction < 55:
                decision = "pass"   # same sub-band rule the champion arm applies
            storage.save_ab_decision(candidate_id=c.cid(), verdict=decision,
                                     conviction=conviction, policy_version=version)
            n += 1
        if n:
            log.info("A/B: recorded %d challenger-arm decision(s) (policy v%d)", n, version)
        return n
    except Exception as exc:  # noqa: BLE001
        log.debug("shadow_challenger_decision failed: %s", exc)
        return 0


def forward_ab_compare(storage, cfg=None) -> dict:
    """Compare champion vs challenger on resolved shared candidates.

    For each shared candidate with a resolved drift d:
      arm contribution = d if the arm said BUY else 0   (it sat out if it passed).
    Returns mean realized drift captured per arm plus counts. Higher is better.
    """
    cfg = cfg or get_config()
    out = {"n": 0, "champion_edge": 0.0, "challenger_edge": 0.0,
           "champion_buys": 0, "challenger_buys": 0, "agree": 0, "note": ""}
    try:
        pairs = storage.ab_pairs()
    except Exception as exc:  # noqa: BLE001
        out["note"] = f"ab_pairs failed: {exc}"
        return out
    if not pairs:
        out["note"] = "no resolved A/B pairs yet"
        return out

    champ_sum = chall_sum = 0.0
    champ_buys = chall_buys = agree = n = 0
    for p in pairs:
        try:
            d = float(p.get("drift_pct"))
        except (TypeError, ValueError):
            continue
        cv = str(p.get("champ_verdict") or "").lower()
        hv = str(p.get("chall_verdict") or "").lower()
        champ_c = d if cv == "buy" else 0.0
        chall_c = d if hv == "buy" else 0.0
        champ_sum += champ_c
        chall_sum += chall_c
        champ_buys += 1 if cv == "buy" else 0
        chall_buys += 1 if hv == "buy" else 0
        agree += 1 if cv == hv else 0
        n += 1
    if n == 0:
        out["note"] = "no usable resolved pairs"
        return out
    out.update({
        "n": n,
        "champion_edge": champ_sum / n,
        "challenger_edge": chall_sum / n,
        "champion_buys": champ_buys,
        "challenger_buys": chall_buys,
        "agree": agree,
        "note": (f"mean realized drift captured per arm over {n} shared resolved "
                 f"candidates; arms agreed on {agree}/{n}"),
    })
    return out
