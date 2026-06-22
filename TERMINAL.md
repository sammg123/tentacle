# TENTACLE on the Bloomberg terminal

A separate project, `bloomberg-terminal` (FastAPI + React, runs locally at http://localhost:3001),
maps in this bot's Alpaca PAPER account and shows it next to other strategies. It is a **passive,
read-only MIRROR**: it reads the account over Alpaca's API and displays it. It never trades, never
queues orders, and never sends anything back to this bot. Nothing in this repo has to run for it to
work, and turning it off changes nothing here.

This file is the single source of truth for the integration. The kid's runtime awareness lives in
`tentacle/terminal.py` (`TERMINAL_AWARENESS`, appended to the decider's system prompt); the mother's
awareness lives in `MOTHER.md`.

## What it reads from TENTACLE

- **Only the Alpaca PAPER account** `PA3ENGT4GREF` (shown on the terminal as account **TENTACLE**),
  through that account's own API key pair. Balance, equity, cash, positions, open/queued orders, and
  the equity history all come straight from Alpaca, so they are always truthful.
- **Source of truth is local SQLite** (`<repo>/data/tentacle.db`). But TENTACLE now also **mirrors**
  its decisions and trades into this same Supabase project, under prefixed tables
  (`tentacle_signals_log`, `tentacle_executions`), so its journal and fills DO surface on the terminal
  (see "Decisions and trades on the terminal" below). The high-volume raw-signal firehose and the
  universe stay SQLite-only and are not mirrored.

## The screens (F-keys)

| Key | Screen | What it shows for TENTACLE |
|-----|--------|----------------------------|
| F1 | SUMMARY | Leaderboard of all mapped accounts ranked by return over 1D / 7D / 30D. TENTACLE is one row; click it to open its Market Data. |
| F2 | MKTDATA | Market data home (quotes / chart / news / portfolio), scoped to the selected account. |
| F3 | MARKETS | Generic market overview (indices, sectors, FX, bonds). |
| F4 | EQUITY | Per-symbol fundamentals / options / financials. |
| F5 | NEWS | Multi-source news (from the selected account's Supabase; empty for TENTACLE). |
| F6 | PORT | Portfolio: holdings, the account summary + 1D/7D/30D deltas, and the **QUEUED ORDERS** tab. |
| F7 | HELP | Help. |
| F8 | SRCH | Symbol search. |
| F9 | MSGS | The quant **signal log** (reads the account's Supabase `signals_log`; empty for TENTACLE). |
| F10 | CALC | Calculator. |
| F11 | MENU | Menu. |
| F12 | TKTX | Manual order ticket + the bot's order blotter. |

Switch the active account with the **PAPER TRADING ACCOUNT** dropdown in the top status bar, or click
a row on F1 SUMMARY. The **SUMMARY** button (and F1) returns to the leaderboard.

## How orders populate, and where

The kid never places an order directly. The kid returns BUY/PASS + conviction; the **risk engine**
sizes it within its hard caps and the broker (`tentacle/broker/alpaca.py`) submits it to the paper
account. From there the terminal shows it automatically:

- **Filled buy** -> appears as a **position** on F6 PORT (holdings) and moves equity + ROI on F1 SUMMARY.
- **Order placed while the market is CLOSED** -> Alpaca holds it as a `day` order in status `accepted`
  (a queued order). It shows in the **QUEUED ORDERS** tab (F6 PORT, and the portfolio panel on F2) and
  on the F12 blotter until it fills at the next open.
- **Close** -> `close_position` flattens the position (Alpaca `DELETE /v2/positions/{symbol}`); the
  holding disappears and realized P&L lands in equity.

### "Queue orders before the open" — the honest version

The queueing mechanism above is real and the terminal surfaces it. But **TENTACLE does not pre-queue
today**: the risk law has a `require_market_open` gate (default `True`, in `tentacle/risk/manager.py`),
so the bot only fires while the market is open and its orders fill intraday rather than sitting queued
overnight. To intentionally pre-queue before the open you would relax that gate, which is part of the
immutable risk law and is intentionally off-limits to the model. (For contrast, the MORDECHAI account
on the terminal does a pre-open sweep, which is why its orders sit `accepted` in the QUEUED tab.)

## Decisions and trades on the terminal (the Supabase mirror)

`tentacle/mirror.py` pushes the bot's journal and trades to this Supabase project every cycle:
- `decisions` (BUY/PASS + conviction + thesis) -> `tentacle_signals_log` -> shows on **F9 MSGS** /
  the signal panel for the TENTACLE account.
- `trades` (entries/exits) -> `tentacle_executions` -> shows on the **F12 blotter**, joined to the
  live Alpaca order by `broker_order_id`.

It is **off unless** `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` are set (same project as MORDECHAI;
writes go to the `tentacle_` prefixed tables so nothing collides). It is one-way and write-only:
idempotent UPSERTs keyed on the bot's own ids, a 90-day **retention cap** that prunes old rows so it
can never grow unbounded, and it is fully guarded so a mirror hiccup never breaks the trading loop.
The terminal reads these tables via its `SUPABASE_*_2` + `SUPABASE_TABLE_PREFIX_2=tentacle_` config.

Telegram (`tentacle/notify/dispatch`) is still the bot's push channel for human pings; the terminal is
the passive dashboard.

## Where the awareness is wired in this repo

- **Kid:** `tentacle/terminal.py` exposes `TERMINAL_AWARENESS`, appended to the decider system prompt in
  `tentacle/brain/decider.py`. It is context only and explicitly tells the kid not to chase leaderboard
  rank (the terminal must never induce over-trading; PASS-by-default still governs).
- **Mother:** `MOTHER.md` documents the terminal for the coach, with one hard rule preserved: the
  process review stays **blind to outcomes**, so the terminal's P&L / ROI / leaderboard is never an
  input to the mother's judgment of the kid.
