"""Classifier: pin each RawSignal to ONE ticker + ONE catalyst category.

Resolution order (per CONTRACTS.md brain/classifier):
  1. raw.catalyst_hint, if the source already told us the category (highest trust).
  2. keyword rules over title+summary.
  3. family default (a weak guess based on the source family).
  4. unknown (never fires).

We emit one ClassifiedSignal per ticker in raw.tickers (usually exactly one).
A signal with no tickers yields nothing: we only trade mapped US tickers.
"""
from __future__ import annotations

from typing import Optional

from ..config import get_config
from ..models import Catalyst, ClassifiedSignal, RawSignal, SourceFamily

# Confidence levels per the contract.
_CONF_HINT = 0.8
_CONF_KEYWORD = 0.7
_CONF_FAMILY = 0.4
_CONF_UNKNOWN = 0.1

# AI/quantum vocabulary; a breakout keyword must co-occur with one of these to
# count as ai_quantum_breakout (so "launches model" about a fashion model does
# not classify as an AI catalyst).
_AI_TERMS = (
    "ai", "a.i.", "artificial intelligence", "llm", "large language model",
    "machine learning", "neural", "gpu", "transformer", "quantum", "qubit",
    "foundation model", "generative",
)


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def _keyword_catalyst(text: str) -> Optional[str]:
    """Map free text to a Catalyst via keyword rules. Order = priority.

    Returns a Catalyst value or None if nothing matches. Honest note: these are
    coarse heuristics over headline text, not a parser. The hint path (1) is far
    more reliable; this is the fallback for sources that do not set a hint.
    """
    t = text.lower()

    # FDA / regulatory approval.
    if _has_any(t, ("fda approval", "fda approves", "fda approved",
                    "fda clearance", "received approval", "grants approval",
                    "marketing authorization")):
        return Catalyst.FDA_APPROVAL.value

    # M&A / takeover target.
    if _has_any(t, ("to acquire", "to be acquired", "agrees to acquire",
                    "merger", "definitive agreement to", "takeover",
                    "tender offer", "buyout", "to merge with")):
        return Catalyst.MNA_TARGET.value

    # Index inclusion (mechanical forced buying).
    if _has_any(t, ("added to s&p", "added to the s&p", "join the s&p",
                    "added to nasdaq", "added to the nasdaq-100",
                    "added to index", "index inclusion", "set to join the s&p",
                    "will be added to")):
        return Catalyst.INDEX_INCLUSION.value

    # Earnings beat / PEAD.
    if _has_any(t, ("beats estimates", "beats expectations", "tops estimates",
                    "tops expectations", "tops earnings", "tops wall street",
                    "beats earnings", "earnings beat", "beat on the top",
                    "beat on revenue", "beats on earnings", "earnings expectations",
                    "raises guidance", "raised full-year", "exceeds estimates",
                    "exceeded expectations")):
        return Catalyst.EARNINGS_BEAT.value

    # Government / federal contract award.
    if _has_any(t, ("awarded a contract", "awarded contract", "wins contract",
                    "secures contract", "contract award", "government contract",
                    "awarded a", "task order", "idiq")) and "contract" in t:
        return Catalyst.GOV_CONTRACT.value

    # Activist / 13D stake.
    if _has_any(t, ("13d", "schedule 13d", "activist", "activist stake",
                    "takes stake", "builds stake", "discloses stake",
                    "calls for", "proxy fight")):
        return Catalyst.ACTIVIST_STAKE.value

    # Insider cluster buying.
    if _has_any(t, ("insider buy", "insider buying", "insider purchase",
                    "form 4", "cluster buy", "directors buy", "ceo buys",
                    "officers purchase")):
        return Catalyst.INSIDER_CLUSTER.value

    # AI / quantum breakout: a launch/breakthrough keyword AND an AI/quantum term.
    breakout_kw = _has_any(t, ("launches model", "launches a model",
                               "open-source", "open source", "open-sources",
                               "new benchmark", "tops benchmark", "breakthrough",
                               "state-of-the-art", "unveils model", "releases model",
                               "announces model"))
    if breakout_kw and _has_any(t, _AI_TERMS):
        return Catalyst.AI_QUANTUM_BREAKOUT.value

    return None


def _family_default(family: str) -> Optional[str]:
    """A weak catalyst guess from the source family alone.

    These are deliberately conservative: the confirmation gate still requires a
    trigger family and >=2 families, so a wrong weak guess cannot fire alone.
    """
    if family == SourceFamily.EARNINGS.value:
        return Catalyst.EARNINGS_BEAT.value
    if family == SourceFamily.INDEX.value:
        return Catalyst.INDEX_INCLUSION.value
    if family == SourceFamily.REGULATORY.value:
        return Catalyst.FDA_APPROVAL.value
    # sec_filing / newswire / news / halt / social / flow / etc. carry no reliable
    # default category on their own; leave them to the keyword/hint paths.
    return None


def classify(raw: RawSignal, cfg=None) -> list[ClassifiedSignal]:
    """Return one ClassifiedSignal per ticker on `raw` (empty if no tickers).

    Never raises. On any unexpected error returns an empty list so a single bad
    item cannot crash the loop or accidentally manufacture a tradeable signal.
    """
    if cfg is None:
        cfg = get_config()
    try:
        tickers = list(getattr(raw, "tickers", None) or [])
        if not tickers:
            return []

        text = f"{raw.title or ''} {raw.summary or ''}"
        family = raw.family or ""

        # Resolve category once (same for every ticker on this item).
        hint = getattr(raw, "catalyst_hint", None)
        if hint:
            catalyst = hint
            confidence = _CONF_HINT
            rationale = "source catalyst hint"
        else:
            kw = _keyword_catalyst(text)
            if kw:
                catalyst = kw
                confidence = _CONF_KEYWORD
                rationale = "keyword match on title+summary"
            else:
                fam = _family_default(family)
                if fam:
                    catalyst = fam
                    confidence = _CONF_FAMILY
                    rationale = f"family default ({family})"
                else:
                    catalyst = Catalyst.UNKNOWN.value
                    confidence = _CONF_UNKNOWN
                    rationale = "no hint, keyword, or family default"

        out: list[ClassifiedSignal] = []
        seen: set[str] = set()
        for tk in tickers:
            tk = (tk or "").upper().strip()
            if not tk or tk in seen:
                continue
            seen.add(tk)
            out.append(ClassifiedSignal(
                raw=raw,
                ticker=tk,
                catalyst=catalyst,
                classify_confidence=confidence,
                rationale=rationale,
            ))
        return out
    except Exception:
        return []
