"""The BRAIN: turns raw signals into scored, judged trade candidates.

Pipeline (see CONTRACTS.md and models.py):

    classifier.classify(raw)            -> list[ClassifiedSignal]
    confirmation.build_candidates(...)  -> list[Candidate]   (grouped by ticker+catalyst)
    confirmation.passes_confirmation_gate(cand) -> (bool, reasons)
    confidence.score_candidate(cand)    -> Candidate         (confidence 0..100)
    judge.judge_candidates(cands)       -> list[Candidate]   (heuristic + optional Claude)

The brain NEVER places an order. It scores and reasons. The risk module owns the
fire/no-fire law. Everything fails safe: unknown data blocks a fire, never allows
one by accident.
"""
