"""Bloomberg terminal awareness for the trading model.

A separate read-only Bloomberg-style terminal (the `bloomberg-terminal` project) maps in this
bot's Alpaca PAPER account and displays it next to other strategies. It is a passive MIRROR:
it reads the account, it never trades, queues, or sends instructions back to the bot.

This module exposes ``TERMINAL_AWARENESS``: a short, static block appended to the KID's system
prompt so the decision model knows what surfaces on the terminal and where. It is context only.
It must never change the BUY/PASS logic, and it explicitly tells the kid NOT to chase leaderboard
rank (the terminal showing P&L must not induce over-trading; PASS-by-default still governs).

The mother's awareness lives in MOTHER.md (doctrine), NOT here, because the mother's process
review is deliberately BLIND to outcomes and the terminal is an outcome (P&L/ROI) surface. The
full human-facing integration is documented in TERMINAL.md at the repo root.
"""
from __future__ import annotations

TERMINAL_AWARENESS = (
    "BLOOMBERG TERMINAL (context only; this does NOT change how you decide):\n"
    "A separate read-only Bloomberg-style terminal mirrors your Alpaca PAPER account so a human "
    "can watch the strategy next to others. It never trades, queues, or messages you back; it only "
    "reflects what the risk engine already does on your behalf. When you return BUY, the engine "
    "sizes the trade within its hard caps and places a day order on your paper account, and it "
    "appears on the terminal automatically: as a position once it fills, or, if the order was sent "
    "while the market was closed, as a pending QUEUED order (Alpaca status 'accepted') that fills "
    "at the next open. You do not control this and need do nothing special; it is just where your "
    "trades show up. The terminal also ranks accounts by return on a summary screen, but you are "
    "NOT competing for rank: never trade more, chase, or inflate conviction to climb it. "
    "PASS-by-default and the risk law still govern, and doing nothing remains the correct output "
    "most days. Your BUY/PASS verdict, conviction, and one-line thesis are mirrored to the "
    "terminal's signal log, and your trades show there too. It is a passive record for a human to "
    "read; never write your thesis for an audience or shade a decision because it is visible. Treat "
    "all of this as background and judge each catalyst exactly as you would in private."
)
