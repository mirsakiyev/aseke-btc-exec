"""Wiring: config -> simulator + adapters + journal + engine; status, heartbeat, operator
control queue and the foreground worker loop. Live mode is refused before any adapter exists.

Single-writer topology for the journal (SQLite WAL-reset-bug mitigation, docs/runbook.md):
the worker process owns the only read-write journal connection. Operator commands issued
while a worker runs are written as atomic control-request files and applied BY the worker;
when no worker runs, the command takes the worker lock and applies the change itself.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import PHASE
from .clock import Clock, SystemClock, iso
from .config import Config
from .engine import TERMINAL, Engine
from .inbox import Inbox
from .journal import Journal
from .lock import LockHeld, ProcessLock
from .num import show
from .sim import SimAdapter, SimExchange

log = logging.getLogger("aseke_exec.runtime")

CONTROL_ACTIONS = ("pause", "resume", "account-disable", "account-enable", "account-unblock",
                   "incident-resolve")


@dataclass
class Runtime:
    config: Config
    clock: Clock
    sim: SimExchange
    journal: Journal
    engine: Engine
    started_at: str = ""

    def close(self) -> None:
        self.journal.close()
        self.sim.close()


def build_runtime(cfg: Config, clock: Clock | None = None, *, busy_timeout_ms: int = 15000) -> Runtime:
    if cfg.mode != "simulation":  # defence in depth; config loading already refuses live
        raise RuntimeError("LIVE_NOT_SUPPORTED: only simulation mode is executable")
    clock = clock or SystemClock()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    sim = SimExchange(cfg.sim.db_path, clock)
    sim.install(cfg.sim)
    journal = Journal(cfg.journal_path, busy_timeout_ms=busy_timeout_ms)
    adapters = {aid: SimAdapter(sim, a.venue, aid) for aid, a in cfg.accounts.items()}
    alerts_file = cfg.data_dir / "alerts.jsonl"

    def sink(rec: dict[str, Any]) -> None:
        try:
            with open(alerts_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            log.warning("cannot append to %s", alerts_file)

    engine = Engine(cfg, journal, adapters, clock, Inbox(cfg.inbox_dir), alert_sink=sink)
    return Runtime(cfg, clock, sim, journal, engine, started_at=iso(clock.now()) or "")


# ------------------------------------------------------------------ operator controls
def apply_control(journal: Journal, req: dict[str, Any], now: Any) -> tuple[str, str]:
    """Apply one operator action with the (single) read-write journal connection."""
    if not isinstance(req, dict) or req.get("action") not in CONTROL_ACTIONS:
        raise ValueError("control request must be an object with a known action")
    for k in ("target", "reason", "note", "request_id", "requested_at"):
        if req.get(k) is not None and not isinstance(req.get(k), str):
            raise ValueError(f"control request field {k} must be a string")
    if req["action"].startswith("account-") or req["action"] == "incident-resolve":
        if not req.get("target"):
            raise ValueError("control request needs a target")
    if req["action"] == "incident-resolve" and not str(req["target"]).isdigit():
        raise ValueError("incident id must be an integer")
    action, target = req.get("action"), req.get("target")
    rid = str(req.get("request_id") or uuid.uuid4().hex)
    if journal.q1("SELECT id FROM operator_actions WHERE request_id=?", (rid,)):
        return "DUPLICATE", "request already applied"
    outcome, msg = "APPLIED", ""
    if action == "pause":
        journal.set_control("pause_new_entries", "1", req.get("reason"), now)
        msg = "new entries PAUSED (protection, exits and reconciliation continue)"
    elif action == "resume":
        journal.set_control("pause_new_entries", "0", req.get("reason"), now)
        msg = "new entries RESUMED"
    elif action in ("account-disable", "account-enable", "account-unblock"):
        if journal.account(str(target)) is None:
            outcome, msg = "REFUSED", f"unknown account {target} (run a cycle first)"
        elif action == "account-disable":
            journal.update("accounts", "account_id", target, operator_disabled=1, updated_at=now)
            msg = f"{target}: disabled for NEW entries; existing exposure stays managed"
        elif action == "account-enable":
            journal.update("accounts", "account_id", target, operator_disabled=0, updated_at=now)
            msg = f"{target}: enabled for new entries"
        else:
            inc = journal.open_incidents(str(target))
            if inc:
                outcome, msg = "REFUSED", f"{len(inc)} open incident(s) on {target}; resolve them first"
            else:
                journal.update("accounts", "account_id", target, entries_blocked=0, block_reason=None,
                               updated_at=now)
                msg = f"{target}: entry block cleared by operator"
    elif action == "incident-resolve":
        journal.update("incidents", "id", int(target), status="RESOLVED", resolved_at=now,  # type: ignore[arg-type]
                       resolution=f"operator: {req.get('note', '')}")
        msg = f"incident {target} resolved"
    else:
        outcome, msg = "REFUSED", f"unknown action {action!r}"
    journal.insert("operator_actions", ts=now, request_id=rid, action=str(action), target=str(target),
                   outcome=outcome, detail={"message": msg, "reason": req.get("reason"),
                                            "requested_at": req.get("requested_at")})
    return outcome, msg


def submit_control(cfg: Config, action: str, target: str | None = None, **extra: Any) -> tuple[str, str]:
    """Apply directly when no worker is running (taking the worker lock); otherwise queue an
    atomic control request for the running worker (keeps the journal single-writer)."""
    now = SystemClock().now()
    req = {"request_id": uuid.uuid4().hex, "action": action, "target": target,
           "requested_at": iso(now), **extra}
    try:
        lock = ProcessLock(cfg.lock_path).acquire()
    except LockHeld:
        cfg.control_dir.mkdir(parents=True, exist_ok=True)
        name = f"{now.strftime('%Y%m%dT%H%M%S%f')}_{req['request_id']}.json"
        tmp = cfg.control_dir / f".{name}.tmp"
        tmp.write_text(json.dumps(req), encoding="utf-8")
        os.replace(tmp, cfg.control_dir / name)
        return "QUEUED", f"worker is running; request {req['request_id']} queued and will be applied " \
                         "at the start of its next cycle (see operator_actions / status)"
    try:
        j = Journal(cfg.journal_path)
        try:
            return apply_control(j, req, now)
        finally:
            j.close()
    finally:
        lock.release()


def drain_controls(rt: Runtime) -> int:
    d = rt.config.control_dir
    if not d.exists():
        return 0
    n = 0
    rejected = d / "rejected"
    for p in sorted(x for x in d.iterdir() if x.is_file() and x.suffix == ".json" and not x.name.startswith(".")):
        try:
            req = json.loads(p.read_text(encoding="utf-8"))
            outcome, msg = apply_control(rt.journal, req, rt.clock.now())
            rt.engine.alert("INFO", "OPERATOR_ACTION", f"{req.get('action')} {req.get('target') or ''}: "
                                                       f"{outcome} {msg}")
            p.unlink()
            n += 1
        except OSError as e:
            log.warning("control request %s not readable yet: %s", p.name, e)
        except Exception as e:  # noqa: BLE001 - malformed request: set aside, never stall cycles
            rejected.mkdir(exist_ok=True)
            try:
                os.replace(p, rejected / p.name)
            except OSError:
                pass
            rt.engine.alert("WARNING", "OPERATOR_ACTION_REJECTED", f"{p.name}: {type(e).__name__}: {e}")
    return n


# ------------------------------------------------------------------ status / heartbeat
def status_report(journal: Journal, limit: int = 20) -> dict[str, Any]:
    def ex_view(e: dict[str, Any]) -> dict[str, Any]:
        return {k: (show(v) if hasattr(v, "is_finite") else v) for k, v in e.items() if k in (
            "id", "trade_id", "account_id", "native_symbol", "direction", "leverage", "state",
            "entry_outcome", "reason_code", "requested_margin", "target_notional", "limit_price",
            "planned_qty", "planned_margin", "filled_qty", "avg_fill", "filled_notional",
            "actual_margin", "reserved_amount", "unused_allocation", "entry_fees", "exit_fees",
            "risk_at_stop", "risk_pct_of_margin", "pnl_at_stop", "protected_qty", "stop_coverage",
            "tp_coverage", "unprotected_since", "max_unprotected_seconds", "tp_fail_count",
            "deadline_at", "close_reason", "realized_pnl", "net_pnl")}

    return {
        "phase": PHASE,
        "controls": {r["key"]: r["value"] for r in journal.q("SELECT * FROM controls")},
        "accounts": journal.q("SELECT * FROM accounts ORDER BY account_id"),
        "active_executions": [ex_view(e) for e in journal.active_executions(TERMINAL)],
        "recent_executions": [ex_view(e) for e in journal.q(
            "SELECT * FROM executions ORDER BY id DESC LIMIT ?", (limit,))],
        "open_incidents": journal.open_incidents(),
        "recent_alerts": journal.q("SELECT ts, level, code, account_id, trade_id, message FROM alerts "
                                   "ORDER BY id DESC LIMIT ?", (limit,)),
        "event_counts": {r["process_status"]: r["n"] for r in journal.q(
            "SELECT process_status, COUNT(*) AS n FROM events GROUP BY process_status")},
    }


def _atomic_write(p: Path, data: dict[str, Any]) -> None:
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, p)


def write_status(rt: Runtime) -> Path:
    p = rt.config.data_dir / "status.json"
    rep = status_report(rt.journal)
    rep["generated_at"] = iso(rt.clock.now())
    _atomic_write(p, rep)
    return p


def write_heartbeat(rt: Runtime, kind: str = "worker") -> None:
    """kind: "worker" (resident `run` loop) or "one-shot" (`cycle`/`reconcile`, exits after)."""
    import sqlite3
    hb = {"kind": kind, "pid": os.getpid(), "started_at": rt.started_at, "written_at": iso(rt.clock.now()),
          "wall_written_at": iso(SystemClock().now()), "cycles": rt.engine.cycles,
          "poll_interval_seconds": rt.config.poll_interval_seconds,
          "config_hash": rt.config.source_hash, "data_dir": str(rt.config.data_dir),
          "sqlite_version": sqlite3.sqlite_version, "metrics": rt.engine.metrics}
    try:
        _atomic_write(rt.config.heartbeat_path, hb)
    except OSError:
        log.warning("heartbeat write failed")


def run_worker(cfg: Config, max_cycles: int | None = None, config_path: Path | None = None,
               poll_seconds: float | None = None) -> int:
    """Foreground worker: single process (lock), startup reconcile, then periodic cycles."""
    from .config import ConfigError, load_config
    from .runtime_policy import require_operational
    require_operational("worker startup")  # before the lock, controls, signals or databases
    poll = cfg.poll_interval_seconds if poll_seconds is None else poll_seconds
    with ProcessLock(cfg.lock_path):
        rt = build_runtime(cfg)
        mtime = config_path.stat().st_mtime if config_path else None
        n = 0
        try:
            try:
                drain_controls(rt)
            except Exception:  # noqa: BLE001 - never prevent the worker from managing exposure
                log.exception("control drain failed at startup")
            rt.engine.startup_reconcile()
            while max_cycles is None or n < max_cycles:
                if config_path and config_path.stat().st_mtime != mtime:
                    mtime = config_path.stat().st_mtime
                    try:  # keep the SAME runtime root on reload
                        rt.engine.apply_config(load_config(config_path, data_dir=rt.config.data_dir))
                        rt.config = rt.engine.cfg
                        rt.engine.alert("INFO", "CONFIG_RELOADED", "configuration reloaded")
                    except ConfigError as e:
                        rt.engine.alert("ERROR", "CONFIG_RELOAD_REJECTED", str(e))
                try:
                    drain_controls(rt)
                except Exception:  # noqa: BLE001 - isolated from the trading cycle
                    log.exception("control drain failed")
                try:
                    rt.engine.run_cycle()
                except Exception:  # keep the worker alive; exits/protection retried next cycle
                    log.exception("cycle failed; continuing")
                    rt.engine.metrics["cycle_errors"] += 1
                write_heartbeat(rt)
                try:
                    write_status(rt)
                except Exception:  # noqa: BLE001 - status is advisory
                    log.exception("status write failed")
                n += 1
                if max_cycles is None or n < max_cycles:
                    time.sleep(poll)
        except KeyboardInterrupt:
            log.info("worker stopped by operator")
        finally:
            rt.close()
    return n
