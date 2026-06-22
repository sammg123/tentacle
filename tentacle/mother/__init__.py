"""The Mother: TENTACLE's meta-learning coach. See MOTHER.md.

She has two eyes and a hand:
  * Eye 2 (guardian): aggregate, noise-gated financial alerts. Sees outcomes.
  * Eye 1 (review): blind process review of the kid's reasoning. Never sees outcomes.
  * Hand (champion): proposes a challenger policy and, when it beats the champion in
    a forward shadow A/B (or, with too few A/B pairs, a weaker process rubric) and we
    are not overcorrecting a winner, promotes it. Meta-calibrates herself.

The heavy pass (review + propose) is gated on enough NEW decisions since the last
review, so she never grades noise. She edits only the kid's reasoning policy
(policy.py), never the risk law.
"""
from __future__ import annotations

from typing import Callable, Optional

from ..config import Config, get_config
from ..storage import Storage, get_storage
from ..util import get_logger

log = get_logger("tentacle.mother")


def run_mother(storage: Optional[Storage] = None, cfg: Optional[Config] = None,
               md=None, notify: Optional[Callable[[str], None]] = None,
               propose: bool = True, promote: bool = False) -> dict:
    """Run one full mother pass. Never raises; returns a combined report."""
    storage = storage or get_storage()
    cfg = cfg or get_config()
    report: dict = {"guardian": None, "review": None, "proposal": None,
                    "promotion": None, "meta": None, "errors": []}

    # Eye 2: financial guardian (aggregate, noise-gated)
    try:
        from .guardian import check_and_alert
        report["guardian"] = check_and_alert(storage, cfg, md, notify=notify)
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"guardian: {exc}")

    # Gate: do not run the heavy blind review until enough NEW decisions have accrued
    # since the last review, so the mother never grades noise on a tiny sample.
    do_review = True
    total = None
    try:
        total = storage.count_decisions()
        last = int(storage.kv_get("mother.last_review_count", 0) or 0)
        new_decisions = total - last
        min_new = int(cfg.mother.min_new_decisions)
        if new_decisions < min_new:
            do_review = False
            report["review"] = {"skipped": True, "new_decisions": new_decisions,
                                "min_new": min_new,
                                "reason": f"only {new_decisions} new decision(s) since "
                                          f"last review (< {min_new}); not enough to judge"}
            report["proposal"] = {"proposed": False,
                                  "reason": "review skipped: insufficient new data"}
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"gate: {exc}")  # on error, proceed (do_review stays True)

    # Eye 1: blind process review of the kid's reasoning
    review = None
    if do_review:
        try:
            from .review import review_decisions
            review = review_decisions(storage, cfg)
            report["review"] = review
            if total is not None:
                storage.kv_set("mother.last_review_count", total)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"review: {exc}")

        # Hand: propose a challenger policy from the review's flaws
        if propose:
            try:
                from .review import propose_challenger
                report["proposal"] = propose_challenger(storage, cfg, review=review)
            except Exception as exc:  # noqa: BLE001
                report["errors"].append(f"propose: {exc}")

    # Hand: promote the challenger only if it clearly wins and we are not overcorrecting
    if promote:
        try:
            from .champion import evaluate_promotion
            report["promotion"] = evaluate_promotion(storage, cfg, md)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"promotion: {exc}")

    # Who watches the mother: did her past changes actually help?
    try:
        from .champion import meta_calibration
        report["meta"] = meta_calibration(storage, cfg, md)
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"meta: {exc}")

    # Graduation: has the kid earned a real-money account? (Only the mother approves.)
    try:
        from .graduation import assess_graduation
        grad = assess_graduation(storage, cfg, md)
        report["graduation"] = grad
        if notify and grad.get("newly_approved"):
            notify("MOTHER APPROVES graduation: the kid has proven itself across the "
                   "paper ladder. A real-money account is unlocked. To go live: set "
                   "ALPACA_PAPER=false with live keys, then arm. Until then it stays paper.")
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"graduation: {exc}")

    try:
        prop = report.get("proposal") or {}
        if notify and prop.get("proposed"):
            notify(f"mother proposed a challenger policy: {prop.get('changelog', '')}")
    except Exception:  # noqa: BLE001
        pass
    return report
