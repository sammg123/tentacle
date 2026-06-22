# Deploying TENTACLE to Railway (always-on, API backend)

The `claude` CLI subscription only works where you are logged into Claude Code (your
Mac). In a container it cannot authenticate, so on Railway the decision-maker uses
the **Anthropic API** (`ANTHROPIC_API_KEY`). Cost is small because the kid only calls
Claude on rare confirmed catalysts (see the cost table in the chat / README).

## 1. Get an Anthropic API key (separate from your Claude subscription)
- console.anthropic.com -> API keys -> create a key (`sk-ant-...`).
- Add a little credit (Billing). At ~1.5 cents/call on Opus and the daily cap below,
  $20 lasts a long time. This is a different wallet from your Claude Pro/Max plan.

## 2. Create the Railway service
```bash
# from the repo root
railway login
railway init            # create a new project
railway up              # builds the Dockerfile and deploys
```

## 3. Add a persistent volume (so learning survives restarts)
In the Railway dashboard: Service -> Variables/Volumes -> add a Volume mounted at
`/data`. The DB lives there via `TENTACLE_DB=/data/tentacle.db` (set below).

## 4. Set environment variables (Railway dashboard -> Variables)
Copy from `.env.railway.example`. The required ones:

```
CONTACT_EMAIL=goldfedergpt@gmail.com
BROKER=alpaca
ALPACA_API_KEY=...            # your Alpaca PAPER key
ALPACA_SECRET_KEY=...         # your Alpaca PAPER secret
ALPACA_PAPER=true             # stays paper until the mother approves graduation
STARTING_CAPITAL=1000

TENTACLE_CLAUDE_BACKEND=api   # use the API, not the (absent) CLI
ANTHROPIC_API_KEY=sk-ant-...  # the key from step 1
TENTACLE_KID_MODEL=claude-sonnet-4-6   # trader: fast + cheaper
TENTACLE_MOTHER_MODEL=claude-opus-4-8  # coach: deep, runs rarely
TENTACLE_LLM_DAILY_CALL_CAP=60   # hard cost ceiling; 0 = unlimited

TENTACLE_DB=/data/tentacle.db    # persist on the volume from step 3
TENTACLE_ARMED=false             # safe: only shadow-logs until you arm AND graduate
```

Never set these as build args or commit them; Railway injects them at runtime, and
the package's .env loader yields to real environment variables.

## 5. The weekly mother pass
The deployed service runs the trading loop (`tentacle run`) and the cheap financial
guardian every cycle. The heavy coaching pass (blind review + propose + promote) runs
on a cadence. Two options:
- **Railway cron:** add a second service / cron from the same image with
  `CMD ["python","-m","tentacle","mother","--promote"]` on a weekly schedule, sharing
  the same `/data` volume and env vars.
- **From your Mac:** run `./mother.sh` weekly against a copy of the DB.

## 6. Going live with real money (later)
The whole ladder is paper. Real money is blocked by the engine until the mother
approves graduation (`tentacle graduate`). Only then: set `ALPACA_PAPER=false` with
live Alpaca keys and `tentacle arm`. Until all of that, it cannot touch real money.
