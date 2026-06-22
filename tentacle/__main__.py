"""TENTACLE command line.

  python -m tentacle doctor     # which tentacles are live vs dormant, and why
  python -m tentacle poll       # poll every live tentacle once (debug)
  python -m tentacle cycle      # run ONE full engine cycle and print the summary
  python -m tentacle run        # run forever, cadence aware of market hours
  python -m tentacle status     # rung, equity, mastery, open trades, recent signals
  python -m tentacle backtest   # Rung -1 gauntlet; on pass, unlocks Rung 0
  python -m tentacle arm        # ARM real/paper firing (off by default)
  python -m tentacle disarm     # safe again
  python -m tentacle revive     # clear a DEATH flag so the bot can re-qualify

Nothing fires unless the bot is ARMED, alive, in-regime, market-open, inside all
risk caps, and at a rung with capital. At rung 0 ($0) it only shadow-logs, which
is the expected default output for most cycles.
"""
from __future__ import annotations

import argparse
import sys
import time

from .config import get_config, rung_by_level
from .storage import get_storage
from .util import get_logger, setup_logging

log = get_logger("tentacle.cli")


def cmd_doctor(args) -> int:
    from .sources.registry import doctor
    rep = doctor()
    live = [r for r in rep if r["enabled"]]
    dormant = [r for r in rep if not r["enabled"]]
    print(f"\nTENTACLE doctor: {len(live)} live tentacle(s), {len(dormant)} dormant\n")
    print("LIVE (free or keyed):")
    for r in live:
        print(f"  [{r['tier']}] {r['id']:<22} {r['family']}")
    print("\nDORMANT (set the env var to light up):")
    for r in dormant:
        print(f"  [{r['tier']}] {r['id']:<22} {r['family']:<16} needs {r['missing_keys']}")
    print()
    return 0


def cmd_poll(args) -> int:
    from .sources.registry import build_sources
    srcs = build_sources(get_storage())
    total = 0
    for s in srcs:
        try:
            n = len(s.poll())
        except Exception as exc:  # noqa: BLE001
            n = 0
            log.warning("%s poll error: %s", s.id, exc)
        total += n
        print(f"  {s.id:<22} {n} new")
    print(f"\n{total} new signal(s) across {len(srcs)} tentacle(s)")
    return 0


def cmd_cycle(args) -> int:
    from .engine.loop import Engine
    eng = Engine()
    summary = eng.run_cycle()
    print("\ncycle summary:")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


def cmd_run(args) -> int:
    from .engine.loop import Engine
    eng = Engine()
    cfg = eng.cfg
    log.info("TENTACLE running. Ctrl-C to stop.")
    try:
        while True:
            try:
                summary = eng.run_cycle()
                log.info("cycle %d: %s", eng.state.cycles, {
                    k: v for k, v in summary.items() if k != "notes"})
                for note in summary.get("notes", []):
                    log.info("  note: %s", note)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("cycle error (continuing): %s", exc)
            open_now = eng.md.is_market_open()
            delay = (cfg.engine.cycle_seconds_market_open if open_now
                     else cfg.engine.cycle_seconds_market_closed)
            time.sleep(delay)
    except KeyboardInterrupt:
        log.info("stopped.")
    return 0


def cmd_status(args) -> int:
    from .engine.state import EngineState
    from .marketdata import get_marketdata
    cfg = get_config()
    st = get_storage()
    state = EngineState(st, cfg)
    state.bootstrap(start_rung=0)
    md = get_marketdata()
    acct = state.account_state(md)
    rung = rung_by_level(state.rung)
    print("\n=== TENTACLE status ===")
    print(f"  rung           {state.rung} ({rung.name}, cap ${rung.capital:.0f})")
    print(f"  alive          {acct.alive}        armed {acct.armed}")
    print(f"  equity         ${acct.equity:.2f}")
    print(f"  cash settled   ${acct.settled_cash:.2f}   unsettled ${acct.unsettled_cash:.2f}")
    print(f"  open positions {acct.open_positions}    realized today ${acct.realized_pnl_today:.2f}")
    print(f"  halted today   {acct.halted_today}   cycles {state.cycles}")
    try:
        from .ladder.mastery import compute_mastery
        mastery, comp = compute_mastery(st, cfg)
        print(f"  mastery        {mastery:.1f}/100  {comp}")
    except Exception:
        pass
    opens = st.open_trades()
    if opens:
        print("\n  open trades:")
        for t in opens:
            print(f"    {t['ticker']:<6} x{t['qty']:.4f} @ {t['entry_price']:.2f} "
                  f"stop {t['stop_price']:.2f} conf {t['confidence']:.0f}")
    closed = st.closed_trades()
    if closed:
        wins = sum(1 for t in closed if (t["pnl"] or 0) > 0)
        pnl = sum(t["pnl"] or 0 for t in closed)
        print(f"\n  closed trades  {len(closed)}  wins {wins}  total pnl ${pnl:.2f}")
    res = st.resolved_outcomes()
    if res:
        ow = sum(1 for o in res if o["label"] == "win")
        print(f"  resolved signals {len(res)}  win {ow}  "
              f"({100*ow/max(1,len(res)):.0f}% incl. shadow)")
    print()
    return 0


def cmd_backtest(args) -> int:
    from .backtest.gauntlet import run_gauntlet
    from .engine.state import EngineState
    cfg = get_config()
    st = get_storage()
    print("Running Rung -1 gauntlet (this hits free historical data; may take a minute)...")
    res = run_gauntlet(cfg)
    print("\ngauntlet result:")
    for k, v in res.items():
        print(f"  {k}: {v}")
    if res.get("passed"):
        state = EngineState(st, cfg)
        state.set_backtest_passed(True)
        if state.rung < 0:
            state.set_rung(0, reset_stage=True)
        print("\nPASSED. Rung 0 (Live Shadow/Paper) unlocked. Run `python -m tentacle run`.")
    else:
        print("\nNot passed yet. The bot stays at Rung -1 (paper math only).")
    return 0


def cmd_arm(args) -> int:
    from .engine.state import EngineState
    state = EngineState(get_storage(), get_config())
    state.bootstrap(start_rung=0)
    state.set_armed(True)
    print("ARMED. The bot may now place orders when ALL gates pass and the rung has capital.")
    return 0


def cmd_disarm(args) -> int:
    from .engine.state import EngineState
    state = EngineState(get_storage(), get_config())
    state.bootstrap(start_rung=0)
    state.set_armed(False)
    print("DISARMED. No orders will be placed; shadow logging continues.")
    return 0


def cmd_journal(args) -> int:
    st = get_storage()
    rows = st.recent_decisions(limit=args.limit, with_outcomes=args.full)
    if not rows:
        print("no decisions journaled yet (the kid has not ruled on a confirmed candidate).")
        return 0
    print(f"\n=== decision journal (last {len(rows)}) ===")
    for r in rows:
        line = (f"  {r['decided_at'][:19]}  {r['ticker']:<6} {r['catalyst']:<15} "
                f"{r['verdict']:<4} conv={r['conviction']:.0f} "
                f"[{r['backend']}] fired={'Y' if r['fired'] else 'n'}")
        if args.full and r.get("outcome_label"):
            d = r.get("outcome_drift")
            line += f"  -> {r['outcome_label']}" + (f" ({d*100:+.1f}%)" if d is not None else "")
        print(line)
        if r.get("thesis"):
            print(f"        thesis: {r['thesis'][:110]}")
    print()
    return 0


def cmd_mother(args) -> int:
    from .mother import run_mother
    from .notify import dispatch
    cfg = get_config()
    st = get_storage()
    print("Running a mother pass (guardian -> blind review -> propose"
          + (" -> promote" if args.promote else "") + ")...\n")
    report = run_mother(st, cfg, notify=lambda m: dispatch(m, cfg),
                        propose=not args.no_propose, promote=args.promote)
    import json as _json
    print(_json.dumps(report, indent=2, default=str)[:4000])
    return 0


def cmd_policy(args) -> int:
    from .policy import get_champion_policy, get_challenger_policy
    st = get_storage()
    champ = get_champion_policy(st)
    chal = get_challenger_policy(st)
    print(f"\n=== champion policy v{champ.get('version')} (by {champ.get('created_by')}) ===")
    print(f"  changelog: {champ.get('changelog', '')}")
    lessons = champ.get("lessons") or []
    if lessons:
        print("  lessons:")
        for les in lessons:
            print(f"    - {les}")
    else:
        print("  lessons: none yet (using the built-in decider prompt)")
    if chal:
        print(f"\n  pending challenger v{chal.get('version')}: {chal.get('changelog','')}")
        for les in (chal.get("lessons") or []):
            print(f"    - {les}")
    print()
    return 0


def cmd_graduate(args) -> int:
    from .mother.graduation import assess_graduation, revoke_graduation, is_graduated
    cfg = get_config()
    st = get_storage()
    if args.revoke:
        revoke_graduation(st)
        print("Graduation approval revoked. Real-money trading is blocked again.")
        return 0
    print("Assessing whether the kid is ready for a real-money account...\n")
    rep = assess_graduation(st, cfg)
    print(f"  approved (real money unlocked): {rep['approved']}")
    print(f"  ready now: {rep['ready']}   mastery: {rep['mastery']}   samples: {rep['samples']}")
    print("\n  criteria:")
    for k, v in (rep.get("criteria") or {}).items():
        print(f"    {'PASS' if v else 'no  '}  {k}")
    if rep.get("reasons"):
        print("\n  still needed:")
        for r in rep["reasons"]:
            print(f"    - {r}")
    if rep["approved"]:
        print("\n  The mother has approved. To actually trade real money: set "
              "ALPACA_PAPER=false with live keys in .env, then `tentacle arm`.")
    else:
        print("\n  Not approved. The bot stays on the $100k paper account.")
    return 0


def cmd_revive(args) -> int:
    from .engine.state import EngineState
    state = EngineState(get_storage(), get_config())
    state.bootstrap(start_rung=0)
    state.set_alive(True)
    print(f"Revived at rung {state.rung} ({rung_by_level(state.rung).name}). "
          "It must re-qualify by mastery to climb again.")
    return 0


def main(argv=None) -> int:
    # .env is already loaded at package import (tentacle/__init__.py), before config
    # was imported, so its values reached the config dataclasses. Call again as a
    # harmless, idempotent safety net.
    from . import load_env_file
    load_env_file()
    setup_logging()
    p = argparse.ArgumentParser(prog="tentacle", description="TENTACLE auto-trader")
    sub = p.add_subparsers(dest="cmd")
    for name, fn in [
        ("doctor", cmd_doctor), ("poll", cmd_poll), ("cycle", cmd_cycle),
        ("run", cmd_run), ("status", cmd_status), ("backtest", cmd_backtest),
        ("arm", cmd_arm), ("disarm", cmd_disarm), ("revive", cmd_revive),
        ("policy", cmd_policy),
    ]:
        sp = sub.add_parser(name)
        sp.set_defaults(func=fn)

    gp = sub.add_parser("graduate")
    gp.add_argument("--revoke", action="store_true",
                    help="clear graduation approval; block real-money trading again")
    gp.set_defaults(func=cmd_graduate)

    jp = sub.add_parser("journal")
    jp.add_argument("--limit", type=int, default=20)
    jp.add_argument("--full", action="store_true", help="join in resolved outcomes")
    jp.set_defaults(func=cmd_journal)

    mp = sub.add_parser("mother")
    mp.add_argument("--promote", action="store_true",
                    help="allow promoting a challenger policy if it clearly wins")
    mp.add_argument("--no-propose", action="store_true",
                    help="guardian + review only, do not propose a challenger")
    mp.set_defaults(func=cmd_mother)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
