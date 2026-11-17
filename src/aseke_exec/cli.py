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
from .auth import build_envelope, generate_key_hex, load_secret, strict_loads
from .clock import SystemClock
from .config import Config, ConfigError, load_config
from .examples import EXAMPLE_KINDS, build_amend, build_cancel, build_close, build_open
from .inbox import SignalExists, publish_event, signal_filename
from .journal import Journal
from .lock import LockHeld, ProcessLock
from .num import show
from .runtime import (
    build_runtime, drain_controls, run_worker, status_report, submit_control, write_heartbeat,
    write_status,
)
from .sim import FAULT_MODES, FAULT_OPS, SimExchange

DEFAULT_CONFIG = "config/sim.toml"


def _cfg(args: argparse.Namespace) -> Config:
    return load_config(args.config, data_dir=args.data_dir)


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
    print(f"OK: configuration valid (mode=simulation, phase {PHASE}, auth={cfg.auth.mode})")
    print(f"  data_dir: {cfg.data_dir}")
    for aid, a in cfg.accounts.items():
        print(f"  account {aid}: venue={a.venue} owner={a.strategy_owner} "
              f"margin/trade={show(a.initial_margin_per_trade)} {a.margin_currency} "
              f"tolerance={show(a.adverse_entry_tolerance)} policies={sorted(map(str, a.allowed_level_policies))} "
              f"entries={'on' if a.enabled_for_entries else 'off'}")
    for sid, accs in cfg.routes.items():
        print(f"  route {sid} -> {', '.join(accs)}")
    for kid, k in cfg.auth.keys.items():
        print(f"  auth key {kid}: producer={k.producer} status={k.status} strategies={sorted(k.strategies)} "
              f"source={'env ' + k.secret_env if k.secret_env else 'file'}")
    for w in cfg.warnings:
        print(f"  WARNING: {w}")
    return 0


def cmd_runtime_info(args: argparse.Namespace) -> int:
    from .ops import runtime_info
    try:
        cfg: Config | None = _cfg(args)
    except ConfigError:
        cfg = None
    _print(runtime_info(cfg))
    return 0


def cmd_init_runtime(args: argparse.Namespace) -> int:
    from .ops import init_runtime
    cfg = _cfg(args)
    try:
        _print(init_runtime(cfg))
    except FileExistsError as e:
        print(f"refused: {e}")
        return 2
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    _setup_logging(cfg, args.verbose)
    n = run_worker(cfg, args.max_cycles, Path(args.config).resolve(), poll_seconds=args.poll_seconds)
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
            try:
                write_heartbeat(rt, kind="one-shot")
                write_status(rt)
            finally:
                rt.close()


def cmd_cycle(args: argparse.Namespace) -> int:
    def go(rt: Any) -> int:
        drain_controls(rt)
        rec = rt.engine.startup_reconcile()
        res = rt.engine.run_cycle()
        _print({"reconcile": rec, "ingested": res["ingested"], "stage_errors": res["errors"]})
        return 0
    return _with_runtime(args, go)


def cmd_reconcile(args: argparse.Namespace) -> int:
    def go(rt: Any) -> int:
        _print({"reconcile": rt.engine.startup_reconcile()})
        return 0
    return _with_runtime(args, go)


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    if not cfg.journal_path.exists():
        print(f"no journal at {cfg.journal_path} (run init-runtime or a cycle first)")
        return 2
    j = Journal(cfg.journal_path, readonly=True)  # reader: cannot write or checkpoint
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
              f"margin={e.get('actual_margin')} stop={e.get('stop_coverage')} tp={e.get('tp_coverage')} "
              f"{e.get('reason_code') or e.get('close_reason') or ''}")
    if rep["open_incidents"]:
        print("OPEN INCIDENTS:")
        for i in rep["open_incidents"]:
            print(f"  #{i['id']} {i['kind']} {i['account_id']}: {i['message']}")
    print("recent alerts:")
    for al in rep["recent_alerts"][:10]:
        print(f"  {al['ts']} {al['level']:<8} {al['code']:<26} {al['account_id'] or '-':<13} {al['message']}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    from .ops import health_report
    rep = health_report(_cfg(args))
    if args.json:
        _print(rep)
    else:
        p, lc = rep["process"], rep["lifecycle"]
        print(f"process running: {p['running']} (heartbeat age {p['heartbeat_age_seconds']}s, pid {p['pid']}, "
              f"cycles {p['cycles']}, last cycle {p['last_cycle_ms']} ms)")
        print(f"lifecycle healthy: {lc['healthy']}")
        for r in lc["reasons"]:
            print(f"  - {r}")
    return 0 if rep["lifecycle"]["healthy"] else 1


def _control(args: argparse.Namespace, action: str, target: str | None = None, **extra: Any) -> int:
    outcome, msg = submit_control(_cfg(args), action, target, **extra)
    print(f"{outcome}: {msg}")
    return 0 if outcome in ("APPLIED", "QUEUED", "DUPLICATE") else 2


def cmd_account(args: argparse.Namespace) -> int:
    return _control(args, f"account-{args.action}", args.account_id)


def cmd_incident_resolve(args: argparse.Namespace) -> int:
    return _control(args, "incident-resolve", str(args.incident_id), note=args.note)


def _signer(cfg: Config) -> tuple[str, bytes]:
    kid = cfg.auth.signer_key_id
    if not kid:
        raise ConfigError(["auth.signer.key_id is not configured (needed for local signing)"])
    secret = load_secret(cfg.auth.keys[kid])
    if secret is None:
        spec = cfg.auth.keys[kid]
        raise ConfigError([f"signing key {kid!r} unavailable: set env {spec.secret_env}" if spec.secret_env
                           else f"signing key {kid!r} unavailable: secret file missing/invalid"])
    return kid, secret


def _publish(cfg: Config, ev: dict[str, Any], signed: bool) -> Path:
    if signed:
        kid, secret = _signer(cfg)
        env = build_envelope(ev, key=secret, key_id=kid, audience=cfg.auth.audience,
                             issued_at=SystemClock().now())
        return publish_event(cfg.inbox_dir, env, filename=signal_filename(ev))
    return publish_event(cfg.inbox_dir, ev)


def cmd_publish_example(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    now = SystemClock().now()
    trade_id = args.trade_id or f"demo-{args.kind}-{now.strftime('%Y%m%d%H%M%S')}"
    producer = cfg.auth.keys[cfg.auth.signer_key_id].producer if args.signed and cfg.auth.signer_key_id else None
    kw = {"producer": producer} if producer else {}
    if args.kind in EXAMPLE_KINDS:
        ev = build_open(now, trade_id, **EXAMPLE_KINDS[args.kind], **kw)  # type: ignore[arg-type]
    elif args.kind in ("close", "cancel", "amend") and not args.trade_id:
        print("--trade-id is required for close/cancel/amend")
        return 2
    elif args.kind == "close":
        ev = build_close(now, trade_id, args.revision, **kw)
    elif args.kind == "cancel":
        ev = build_cancel(now, trade_id, args.revision, **kw)
    else:
        ev = build_amend(now, trade_id, args.revision, stop=args.stop or "64100.10",
                         take_profits=[(args.tp or "66000.10", "1")], **kw)
    try:
        p = _publish(cfg, ev, args.signed)
    except SignalExists as e:
        print(str(e))
        return 2
    print(f"published {'SIGNED ' if args.signed else ''}{ev['event_type']} {ev['event_id']} "
          f"(trade {trade_id}) -> {p}")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    if args.action == "gen-key":
        if not args.out:
            print("--out PATH is required (store it in an excluded location such as secrets\\)")
            return 2
        out = Path(args.out)
        if out.exists():
            print(f"refused: {out} exists (never overwrite key material)")
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        repo = Path(__file__).resolve().parents[2]
        resolved = out.resolve()
        if repo in resolved.parents and (repo / "secrets") not in resolved.parents:
            print(f"WARNING: {resolved} is inside the repository but not under the git-ignored "
                  "secrets/ directory - make sure it can never be committed")
        fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # owner-only on POSIX
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(generate_key_hex() + "\n")
        print(f"wrote a new random 256-bit HMAC key to {out} (contents not printed). "
              "Load it into the configured env var without echoing it, e.g. PowerShell: "
              f"$env:<SECRET_ENV> = (Get-Content -Raw '{out}').Trim()")
        return 0
    cfg = _cfg(args)
    if args.action == "check":
        _print({"mode": cfg.auth.mode, "audience": cfg.auth.audience,
                "keys": {k: {"status": v.status, "producer": v.producer,
                             "secret_available": load_secret(v) is not None,
                             "secret_source": f"env {v.secret_env}" if v.secret_env else "file"}
                         for k, v in cfg.auth.keys.items()}})
        return 0
    # sign
    if not args.infile:
        print("--in FILE (unsigned v1 event JSON) is required")
        return 2
    ev = strict_loads(Path(args.infile).read_text(encoding="utf-8"))
    try:
        p = _publish(cfg, ev, signed=True)
    except SignalExists as e:
        print(str(e))
        return 2
    print(f"published SIGNED {ev.get('event_type')} {ev.get('event_id')} -> {p}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from .ops import backup_runtime
    try:
        _print(backup_runtime(_cfg(args), Path(args.dest)))
    except (FileExistsError, FileNotFoundError, ValueError) as e:
        print(f"refused: {e}")
        return 2
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from .ops import restore_runtime
    if not args.data_dir:
        print("refused: --data-dir NEW_ROOT is required (restore never targets the default runtime)")
        return 2
    try:
        _print(restore_runtime(Path(args.backup), _cfg(args)))
    except (FileExistsError, FileNotFoundError, ValueError) as e:
        print(f"refused: {e}")
        return 2
    print("next: run `cycle` (or `run`) with the same --data-dir; startup reconciliation runs "
          "before any new entry")
    return 0


def cmd_soak(args: argparse.Namespace) -> int:
    from .soak import run_soak
    if not args.data_dir:
        print("refused: --data-dir NEW_ROOT is required (soaks never use an existing runtime)")
        return 2
    logging.disable(logging.WARNING)
    try:
        rep = run_soak(args.config, args.data_dir, duration_seconds=args.duration_seconds,
                       cycles=args.cycles, accelerated=args.accelerated, poll_seconds=args.poll_seconds,
                       seed=args.seed, progress_every_seconds=args.progress_every_seconds)
    except (FileExistsError, ValueError) as e:
        print(f"refused: {e}")
        return 2
    _print(rep)
    st = rep.get("storage", {})
    storage_ok = all(v.get("integrity_check") == "ok" and v.get("foreign_key_violations") == 0
                     for v in st.values())
    if not rep["completed"]:
        print(f"SOAK {rep['status']}: partial evidence only (elapsed {rep['elapsed_wall_seconds']} s); "
              "an interrupted run is NOT a completed soak")
        return 5
    return 0 if (not rep["invariant_violations_total"] and storage_ok) else 1


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


def cmd_sim(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    if args.action == "init" and args.reset:
        from .ops import journal_has_history
        try:
            lock = ProcessLock(cfg.lock_path).acquire()
        except LockHeld as e:
            print(f"refused: {e} (never reset an active runtime)")
            return 2
        try:
            if journal_has_history(cfg):
                print(f"refused: the execution journal in {cfg.data_dir} references simulated orders/"
                      "positions; a simulator-only reset would leave it inconsistent. Initialise a fresh "
                      "runtime instead: init-runtime --data-dir <NEW_ROOT>")
                return 2
            sim = SimExchange(cfg.sim.db_path, SystemClock())
            try:
                sim.install(cfg.sim, reset=True)
            finally:
                sim.close()
        finally:
            lock.release()
        print(f"simulator reset and initialised at {cfg.sim.db_path} (journal had no history)")
        return 0
    if args.action == "show":
        sim = SimExchange(cfg.sim.db_path, SystemClock())
        try:
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
        finally:
            sim.close()
        return 0
    # mutating harness commands: take the simulator-harness lock (backup quiesces it)
    try:
        hl = ProcessLock(cfg.sim_lock_path).acquire()
    except LockHeld as e:
        print(f"refused: {e} (a backup/maintenance holds the simulator)")
        return 3
    sim = SimExchange(cfg.sim.db_path, SystemClock())
    try:
        if args.action == "init":
            sim.install(cfg.sim)
            print(f"simulator initialised at {cfg.sim.db_path}")
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
        hl.release()
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aseke-exec",
                                description="ASEKE BTC Exec - SIMULATION ONLY execution worker")
    p.add_argument("--config", default=os.environ.get("ASEKE_EXEC_CONFIG", DEFAULT_CONFIG))
    p.add_argument("--data-dir", default=os.environ.get("ASEKE_EXEC_DATA_DIR"),
                   help="isolated runtime root (overrides the config's data_dir)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate-config", help="validate configuration").set_defaults(fn=cmd_validate)
    sub.add_parser("runtime-info", help="Python/SQLite/dependency/source/config fingerprint (no secrets)"
                   ).set_defaults(fn=cmd_runtime_info)
    sub.add_parser("init-runtime", help="create a FRESH isolated runtime root (refuses non-empty)"
                   ).set_defaults(fn=cmd_init_runtime)
    r = sub.add_parser("run", help="run the worker in the foreground (Ctrl+C to stop)")
    r.add_argument("--max-cycles", type=int, default=None)
    r.add_argument("--poll-seconds", type=float, default=None)
    r.set_defaults(fn=cmd_run)
    sub.add_parser("cycle", help="startup reconcile + one ingestion/maintenance cycle").set_defaults(fn=cmd_cycle)
    sub.add_parser("reconcile", help="reconcile accounts against the simulated venues").set_defaults(fn=cmd_reconcile)
    st = sub.add_parser("status", help="controls, accounts, executions, incidents, alerts (read-only)")
    st.add_argument("--json", action="store_true")
    st.add_argument("--limit", type=int, default=20)
    st.set_defaults(fn=cmd_status)
    he = sub.add_parser("health", help="process heartbeat vs lifecycle health (exit 0 healthy, 1 not)")
    he.add_argument("--json", action="store_true")
    he.set_defaults(fn=cmd_health)
    for name in ("pause", "resume"):
        pp = sub.add_parser(name, help=f"{name} NEW entries (exits/protection continue)")
        pp.add_argument("--reason", default="operator")
        pp.set_defaults(fn=lambda a, n=name: _control(a, n, reason=a.reason))
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
    pe.add_argument("--signed", action="store_true", help="wrap in an HMAC envelope (auth.signer key)")
    pe.set_defaults(fn=cmd_publish_example)
    au = sub.add_parser("auth", help="local simulation key management and signing (keys never printed)")
    au.add_argument("action", choices=["gen-key", "check", "sign"])
    au.add_argument("--out")
    au.add_argument("--in", dest="infile")
    au.set_defaults(fn=cmd_auth)
    bk = sub.add_parser("backup", help="quiesced SQLite-API backup of journal+simulator to a NEW directory")
    bk.add_argument("--dest", required=True)
    bk.set_defaults(fn=cmd_backup)
    rs = sub.add_parser("restore", help="restore a backup into a NEW --data-dir (never over a live runtime)")
    rs.add_argument("--backup", required=True)
    rs.set_defaults(fn=cmd_restore)
    so = sub.add_parser("soak", help="bounded soak in a NEW --data-dir (real-time or --accelerated)")
    g = so.add_mutually_exclusive_group(required=True)
    g.add_argument("--duration-seconds", type=float)
    g.add_argument("--cycles", type=int)
    so.add_argument("--accelerated", action="store_true", help="injected clock (NOT a real-time soak)")
    so.add_argument("--poll-seconds", type=float, default=0.5)
    so.add_argument("--seed", type=int, default=7)
    so.add_argument("--progress-every-seconds", type=float, default=60.0,
                    help="wall-clock interval for soak-progress.json / soak-checkpoints.jsonl")
    so.set_defaults(fn=cmd_soak)
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


#: Commands that stay usable on an unapproved runtime: help, configuration and database-
#: independent diagnostics, plus the READ-ONLY status/health inspectors (mode=ro connections never
#: write pages or checkpoint). Everything else - anything that can create runtime files, open an
#: execution database for writing, or process signals/controls - is refused (exit 4) BEFORE it runs.
_DIAGNOSTIC = {("validate-config", None), ("runtime-info", None), ("status", None), ("health", None),
               ("auth", "gen-key"), ("auth", "check"), ("scenario", "list")}


def _runtime_gate(args: argparse.Namespace) -> int | None:
    from .runtime_policy import current
    key = (args.cmd, getattr(args, "action", None) if args.cmd in ("auth", "scenario") else None)
    st = current()
    if key in _DIAGNOSTIC:
        if not st.operational:
            print(f"WARNING: runtime not approved for operation ({st.wal_reset_fix}; "
                  f"{sys.executable}) - diagnostics only", file=sys.stderr)
        return None
    if st.operational:
        return None
    print(f"refused: `{args.cmd}` needs the approved project runtime (config/runtime-lock.json).")
    print(f"  interpreter: {sys.executable}")
    print(f"  loaded SQLite: {st.probe.get('sqlite_version')} ({st.probe.get('sqlite_source_id')})")
    print(f"  WAL-reset fix: {st.wal_reset_fix}; approved build: {st.approved_build}")
    for r in st.reasons:
        print(f"  - {r}")
    print(r"  Use .\scripts\aseke-exec.ps1 (after .\scripts\setup.ps1); diagnostics: runtime-info")
    return 4


def main(argv: list[str] | None = None) -> int:
    from .runtime_policy import RuntimeNotApproved
    args = build_parser().parse_args(argv)
    gate = _runtime_gate(args)
    if gate is not None:
        return gate
    try:
        return int(args.fn(args) or 0)
    except RuntimeNotApproved as e:
        print(f"refused: {e}")
        return 4
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
