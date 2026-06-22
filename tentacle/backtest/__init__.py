"""The Rung -1 backtest gauntlet.

A full free-news backtest is infeasible (we cannot replay years of every RSS
feed, halt tape, and 13D in their original arrival order). So the gauntlet is an
HONEST proxy with two independent legs:

  1) EARNINGS DRIFT (PEAD): a real, point-in-time study over a liquid basket using
     yfinance earnings dates and forward returns. This validates the core drift
     hypothesis for the earnings_beat catalyst and seeds its drift prior in storage.
  2) RISK / SIZING MONTE CARLO: a synthetic simulation of the confidence->size
     bands plus the stop / target / ruin rules, proving the engine cannot breach
     the ruin floor and that expectancy is positive given a plausible edge.

It is NOT a claim of future returns. It seeds priors and sanity-checks the math.
The notes returned by run_gauntlet always label what is real vs proxied.
"""
from __future__ import annotations

from .gauntlet import run_gauntlet

__all__ = ["run_gauntlet"]
