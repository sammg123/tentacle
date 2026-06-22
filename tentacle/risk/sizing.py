"""Position sizing: how many dollars (and shares) to commit to a candidate.

The base size is a confidence -> fraction band of the CURRENT RUNG's capital
(config.size_fraction_for). That dollar figure is then squeezed by three independent
ceilings, whichever bites first:
  1. the per-trade max position fraction of rung capital (cfg.risk.max_position_fraction),
  2. available SETTLED cash (cash accounts only; never deploy unsettled proceeds),
  3. a fractional-Kelly ceiling (cfg.risk.kelly_fraction_cap of rung capital).

Quantity is fractional (dollars / price, 4 dp, min $1 notional) so a micro rung
can still take a position in a high-priced name. If the price is unknown we
return (0.0, 0.0): FAIL SAFE, no blind sizing.
"""
from __future__ import annotations

import math
from typing import Optional

from ..models import AccountState
from .cash import available_settled_cash


def position_size(score: float, account: AccountState, cfg=None, md=None,
                  ticker: Optional[str] = None,
                  price: Optional[float] = None) -> tuple[float, float]:
    """Return (dollars, qty) for a candidate of confidence ``score``.

    Never raises. Unknown price -> (0.0, 0.0).
    """
    try:
        if cfg is None:
            from ..config import get_config
            cfg = get_config()

        from ..config import size_fraction_for

        try:
            score = float(score)
        except (TypeError, ValueError):
            return (0.0, 0.0)

        try:
            rung_capital = float(account.rung_capital)
        except (TypeError, ValueError, AttributeError):
            return (0.0, 0.0)
        if rung_capital <= 0:
            return (0.0, 0.0)

        # Base size: the confidence band fraction of rung capital.
        frac = float(size_fraction_for(score))
        if frac <= 0:
            return (0.0, 0.0)
        dollars = frac * rung_capital

        # Ceiling 1: per-trade max position fraction of rung capital.
        try:
            max_frac = float(cfg.risk.max_position_fraction)
        except Exception:
            max_frac = 1.0
        dollars = min(dollars, max_frac * rung_capital)

        # Ceiling 2: fractional-Kelly cap (fraction of rung capital).
        try:
            kelly_cap = float(cfg.risk.kelly_fraction_cap)
        except Exception:
            kelly_cap = 0.50
        if kelly_cap > 0:
            dollars = min(dollars, kelly_cap * rung_capital)

        # Ceiling 3: available settled cash (cash accounts only).
        try:
            cash_account = bool(cfg.risk.cash_account)
        except Exception:
            cash_account = True
        if cash_account:
            dollars = min(dollars, available_settled_cash(account))

        if dollars <= 0:
            return (0.0, 0.0)

        # Resolve a price: explicit arg first, then marketdata.
        px = None
        if price is not None:
            try:
                px = float(price)
            except (TypeError, ValueError):
                px = None
        if px is None and ticker:
            if md is None:
                from ..marketdata import get_marketdata
                md = get_marketdata()
            try:
                p = md.last_price(ticker)
                px = float(p) if p is not None else None
            except Exception:
                px = None

        # Unknown price: FAIL SAFE, no sizing.
        if px is None or px != px or px <= 0:
            return (0.0, 0.0)

        # Fractional shares: a $25 micro rung cannot buy a whole $300 share, so we
        # size fractionally (Alpaca supports fractional qty). 4 dp is ample; require
        # at least a $1 notional (Alpaca's fractional minimum).
        qty = math.floor((dollars / px) * 10000) / 10000.0
        if qty <= 0 or qty * px < 1.0:
            return (0.0, 0.0)

        actual_dollars = qty * px
        return (float(actual_dollars), float(qty))
    except Exception:
        return (0.0, 0.0)
