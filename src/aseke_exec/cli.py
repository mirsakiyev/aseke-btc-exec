"""aseke-exec command line (simulation only)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from . import PHASE, __version__
from .clock import SystemClock
from .config import Config, ConfigError, load_config
from .examples import EXAMPLE_KINDS, build_amend, build_cancel, build_close, build_open
from .inbox import SignalExists, publish_event
from .journal import Journal
from .lock import LockHeld, ProcessLock
from .num import show
from .runtime import build_runtime, run_worker, status_report, write_status
from .sim import FAULT_MODES, FAULT_OPS, SimExchange

DEFAULT_CONFIG = "config/sim.toml"


def _cfg(args: argparse.Namespace) -> Config:
    return load_config(args.config)


def _setup_logging(cfg: Config | None, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if cfg is not None:
        (cfg.data_dir / "logs").mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(cfg.data_dir / "logs" / "exec.log", encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ------------------------------------------------------------------ commands
def cmd_validate(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    print(f"OK: configuration valid (mode=simulation, phase {PHASE})")
    print(f"  data_dir: {cfg.data_dir}")
    for aid, a in cfg.accounts.items():
        print(f"  account {aid}: venue={a.venue} owner={a.strategy_owner} "
              f"margin/trade={show(a.initial_margin_per_trade)} {a.margin_currency} "
              f"tolerance={show(a.adverse_entry_tolerance)} policies={sorted(map(str, a.allowed_level_policies))} "
              f"entries={'on' if a.enabled_for_entries else 'off'}")
    for sid, accs in cfg.routes.items():
        print(f"  route {sid} -> {', '.join(accs)}")
    for w in cfg.warnings:
        print(f"  WARNING: {w}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    _setup_logging(cfg, args.verbose)
    n = run_worker(cfg, args.max_cycles, Path(args.config).resolve())
    print(f"worker exited after {n} cycle(s)")
    return 0


def _with_runtime(args: argparse.Namespace, fn: Any) -> int:
    cfg = _cfg(args)
    _setup_logging(cfg, args.verbose)
    with ProcessLock(cfg.lock_path):
        rt = build_runtime(cfg, SystemClock())
        try:
            return fn(rt)
        finally:
            write_status(rt)
            rt.close()


def cmd_cycle(args: argparse.Namespace) -> int:
    def go(rt: Any) -> int:
        rec = rt.engine.startup_reconcile()
        res = rt.engine.run_cycle()
        _print({"reconcile": rec, "ingested": res["ingested"]})
        return 0
    return _with_runtime(args, go)


def cmd_reconcile(args: argparse.Namespace) -> int:
    def go(rt: Any) -> int:
        _print({"reconcile": rt.engine.startup_reconcile()})
        return 0
    return _with_runtime(args, go)


def _journal(args: argparse.Namespace) -> Journal:
    return Journal(_cfg(args).journal_path)


def cmd_status(args: argparse.Namespace) -> int:
    j = _journal(args)
    try:
        rep = status_report(j, args.limit)
    finally:
        j.close()
    if args.json:
        _print(rep)
        return 0
    paused = rep["controls"].get("pause_new_entries") == "1"
    print(f"ASEKE BTC Exec {__version__} | phase {PHASE} | new entries: {'PAUSED' if paused else 'enabled'}")
    print("accounts:")
    for a in rep["accounts"]:
        flags = []
        if a["entries_blocked"]:
            flags.append(f"BLOCKED ({a['block_reason']})")
        if a["operator_disabled"]:
            flags.append("DISABLED")
        print(f"  {a['account_id']:<14} owner={a['strategy_owner']:<18} {' '.join(flags) or 'ok'}")
    print("executions (most recent first):")
    for e in rep["recent_executions"]:
        print(f"  #{e['id']:<3} {e['trade_id']:<24} {e['account_id']:<13} {e['direction']:<5} "
              f"{e['state']:<16} fill={e.get('filled_qty')}@{e.get('avg_fill')} "
              f"margin={e.get('actual_margin')} risk@SL={e.get('risk_at_stop')} "
              f"{e.get('reason_code') or e.get('close_reason') or ''}")
    if rep["open_incidents"]:
        print("OPEN INCIDENTS:")
        for i in rep["open_incidents"]:
            print(f"  #{i['id']} {i['kind']} {i['account_id']}: {i['message']}")
    print("recent alerts:")
    for al in rep["recent_alerts"][:10]:
        print(f"  {al['ts']} {al['level']:<8} {al['code']:<26} {al['account_id'] or '-':<13} {al['message']}")
    return 0


def cmd_pause(args: argparse.Namespace, value: str) -> int:
    j = _journal(args)
    try:
        j.set_control("pause_new_entries", value, args.reason, SystemClock().now())
    finally:
        j.close()
    print("new entries PAUSED (protection, exits and reconciliation continue)" if value == "1"
          else "new entries RESUMED")
    return 0


def cmd_account(args: argparse.Namespace) -> int:
    j = _journal(args)
    now = SystemClock().now()
    try:
        if j.account(args.account_id) is None:
            print(f"unknown account {args.account_id} (run a cycle first)")
            return 2
        if args.action == "disable":
            j.update("accounts", "account_id", args.account_id, operator_disabled=1, updated_at=now)
            print(f"{args.account_id}: disabled for NEW entries; existing exposure stays managed")
        elif args.action == "enable":
            j.update("accounts", "account_id", args.account_id, operator_disabled=0, updated_at=now)
            print(f"{args.account_id}: enabled for new entries")
        else:
            inc = j.open_incidents(args.account_id)
            if inc:
                print(f"refused: {len(inc)} open incident(s) on {args.account_id}; resolve them first")
                return 2
            j.update("accounts", "account_id", args.account_id, entries_blocked=0, block_reason=None,
                     updated_at=now)
            print(f"{args.account_id}: entry block cleared by operator")
    finally:
        j.close()
    return 0


def cmd_incident_resolve(args: argparse.Namespace) -> int:
    j = _journal(args)
    try:
        j.update("incidents", "id", args.incident_id, status="RESOLVED",
                 resolved_at=SystemClock().now(), resolution=f"operator: {args.note}")
    finally:
        j.close()
    print(f"incident {args.incident_id} resolved")
    return 0


def cmd_publish_example(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    now = SystemClock().now()
    trade_id = args.trade_id or f"demo-{args.kind}-{now.strftime('%Y%m%d%H%M%S')}"
    if args.kind in EXAMPLE_KINDS:
        ev = build_open(now, trade_id, **EXAMPLE_KINDS[args.kind])  # type: ignore[arg-type]
    elif args.kind == "close":
        ev = build_close(now, trade_id, args.revision)
    elif args.kind == "cancel":
        ev = build_cancel(now, trade_id, args.revision)
    elif args.kind == "amend":
        ev = build_amend(now, trade_id, args.revision, stop=args.stop or "64100.10",
                         take_profits=[(args.tp or "66000.10", "1")])
    else:
        print(f"unknown kind {args.kind}")
        return 2
    if args.kind in ("close", "cancel", "amend") and not args.trade_id:
        print("--trade-id is required for close/cancel/amend")
        return 2
    try:
        p = publish_event(cfg.inbox_dir, ev)
    except SignalExists as e:
        print(str(e))
        return 2
    print(f"published {ev['event_type']} {ev['event_id']} (trade {trade_id}) -> {p}")
    return 0


def cmd_scenario(args: argparse.Namespace) -> int:
    from .scenarios import SCENARIOS, run_scenario
    logging.disable(logging.CRITICAL)
    if args.action == "list":
        for n, sc in SCENARIOS.items():
            print(f"  {n:<40} {(sc.fn.__doc__ or '').strip().splitlines()[0] if sc.fn.__doc__ else ''}")
        return 0
    names = list(SCENARIOS) if not args.names or args.names == ["all"] else args.names
    failed = 0
    for n in names:
        if n not in SCENARIOS:
            print(f"UNKNOWN {n}")
            failed += 1
            continue
        try:
            res = run_scenario(n)
            print(f"PASS {n:<40} {json.dumps(res)}")
        except Exception as e:  # report every scenario
            failed += 1
            print(f"FAIL {n:<40} {type(e).__name__}: {e}")
    print(f"{len(names) - failed}/{len(names)} scenarios passed")
    return 1 if failed else 0


def _sim(args: argparse.Namespace) -> tuple[Config, SimExchange]:
    cfg = _cfg(args)
    sim = SimExchange(cfg.sim.db_path, SystemClock())
    return cfg, sim


def cmd_sim(args: argparse.Namespace) -> int:
    cfg, sim = _sim(args)
    try:
        if args.action == "init":
            if args.reset:
                try:
                    lock = ProcessLock(cfg.lock_path).acquire()
                except LockHeld as e:
                    print(f"refused: {e}")
                    return 2
                lock.release()
            sim.install(cfg.sim, reset=args.reset)
            print(f"simulator {'reset and ' if args.reset else ''}initialised at {cfg.sim.db_path}")
        elif args.action == "show":
            out: dict[str, Any] = {}
            for v in cfg.sim.venues:
                for sym in sim.symbols(v):
                    q = sim.quote(v, sym)
                    out[f"{v}:{sym}"] = {"last": show(q["last"]), "mark": show(q["mark"]),
                                         "index": show(q["index"]),
                                         "best_bid": show(q["bids"][0][0]) if q["bids"] else None,
                                         "best_ask": show(q["asks"][0][0]) if q["asks"] else None}
            for aid, a in cfg.sim.accounts.items():
                b = sim.balance(a.venue, aid)
                out[aid] = {"wallet": show(b["wallet"]), "available": show(b["available"]),
                            "positions": {s_: {k: show(v_) if hasattr(v_, "is_finite") else v_
                                               for k, v_ in p.items()}
                                          for s_, p in sim.positions(a.venue, aid)},
                            "working_orders": [dict(kind=o["kind"], side=o["side"], qty=o["qty"],
                                                    trigger=o["trigger_price"], cid=o["client_order_id"])
                                               for o in sim.all_orders(a.venue, aid) if o["status"] == "OPEN"]}
            _print(out)
        elif args.action == "set-market":
            venues = list(cfg.sim.venues) if args.venue == "all" else [args.venue]
            for v in venues:
                for sym in sim.symbols(v):
                    sim.set_simple_market(v, sym, args.price, depth_btc=args.depth_btc)
            print(f"market set to {args.price} on {', '.join(venues)} (exchange-held triggers evaluated)")
        elif args.action == "fault":
            sim.add_fault(args.venue, args.op, args.mode, account=args.account, param=args.param,
                          count=args.count)
            print(f"fault armed: {args.venue} {args.op} {args.mode} x{args.count}")
    finally:
        sim.close()
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aseke-exec",
                                description="ASEKE BTC Exec - Phase 1 SIMULATION ONLY execution worker")
    p.add_argument("--config", default=os.environ.get("ASEKE_EXEC_CONFIG", DEFAULT_CONFIG))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate-config", help="validate configuration").set_defaults(fn=cmd_validate)
    r = sub.add_parser("run", help="run the worker in the foreground (Ctrl+C to stop)")
    r.add_argument("--max-cycles", type=int, default=None)
    r.set_defaults(fn=cmd_run)
    sub.add_parser("cycle", help="startup reconcile + one ingestion/maintenance cycle").set_defaults(fn=cmd_cycle)
    sub.add_parser("reconcile", help="reconcile accounts against the simulated venues").set_defaults(fn=cmd_reconcile)
    st = sub.add_parser("status", help="show controls, accounts, executions, incidents, alerts")
    st.add_argument("--json", action="store_true")
    st.add_argument("--limit", type=int, default=20)
    st.set_defaults(fn=cmd_status)
    for name, val in (("pause", "1"), ("resume", "0")):
        pp = sub.add_parser(name, help=f"{name} NEW entries (exits/protection continue)")
        pp.add_argument("--reason", default="operator")
        pp.set_defaults(fn=lambda a, v=val: cmd_pause(a, v))
    ac = sub.add_parser("account", help="disable/enable new entries or clear an entry block")
    ac.add_argument("action", choices=["disable", "enable", "unblock"])
    ac.add_argument("account_id")
    ac.set_defaults(fn=cmd_account)
    inc = sub.add_parser("incident", help="resolve an incident after operator review")
    inc.add_argument("action", choices=["resolve"])
    inc.add_argument("incident_id", type=int)
    inc.add_argument("--note", required=True)
    inc.set_defaults(fn=cmd_incident_resolve)
    pe = sub.add_parser("publish-example", help="atomically publish a dummy SIMULATION signal")
    pe.add_argument("kind", choices=sorted(EXAMPLE_KINDS) + ["close", "cancel", "amend"])
    pe.add_argument("--trade-id")
    pe.add_argument("--revision", type=int, default=2)
    pe.add_argument("--stop")
    pe.add_argument("--tp")
    pe.set_defaults(fn=cmd_publish_example)
    sc = sub.add_parser("scenario", help="run deterministic acceptance scenarios (temp dirs)")
    sc.add_argument("action", choices=["list", "run"])
    sc.add_argument("names", nargs="*")
    sc.set_defaults(fn=cmd_scenario)
    sm = sub.add_parser("sim", help="simulated exchange harness (works while the worker is down)")
    sm.add_argument("action", choices=["init", "show", "set-market", "fault"])
    sm.add_argument("--reset", action="store_true")
    sm.add_argument("--venue", default="all")
    sm.add_argument("--price")
    sm.add_argument("--depth-btc", default="1")
    sm.add_argument("--op", choices=FAULT_OPS)
    sm.add_argument("--mode", choices=FAULT_MODES)
    sm.add_argument("--account")
    sm.add_argument("--param")
    sm.add_argument("--count", type=int, default=1)
    sm.set_defaults(fn=cmd_sim)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except ConfigError as e:
        print("CONFIGURATION INVALID:")
        for pr in e.problems:
            print(f"  - {pr}")
        return 2
    except LockHeld as e:
        print(f"refused: {e}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
