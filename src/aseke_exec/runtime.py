"""Wiring: config -> simulator + adapters + journal + engine. Also status reporting
and the foreground worker loop. Live mode is refused before any adapter exists."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .clock import Clock, SystemClock, iso
from .config import Config
from .engine import TERMINAL, Engine
from .inbox import Inbox
from .journal import Journal
from .lock import ProcessLock
from .num import show
from .sim import SimAdapter, SimExchange

log = logging.getLogger("aseke_exec.runtime")


@dataclass
class Runtime:
    config: Config
    clock: Clock
    sim: SimExchange
    journal: Journal
    engine: Engine

    def close(self) -> None:
        self.journal.close()
        self.sim.close()


def build_runtime(cfg: Config, clock: Clock | None = None) -> Runtime:
    if cfg.mode != "simulation":  # defence in depth; config loading already refuses live
        raise RuntimeError("LIVE_NOT_SUPPORTED: only simulation mode is executable in Phase 1")
    clock = clock or SystemClock()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    sim = SimExchange(cfg.sim.db_path, clock)
    sim.install(cfg.sim)
    journal = Journal(cfg.journal_path)
    adapters = {aid: SimAdapter(sim, a.venue, aid) for aid, a in cfg.accounts.items()}
    alerts_file = cfg.data_dir / "alerts.jsonl"

    def sink(rec: dict[str, Any]) -> None:
        with open(alerts_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    engine = Engine(cfg, journal, adapters, clock, Inbox(cfg.inbox_dir), alert_sink=sink)
    return Runtime(cfg, clock, sim, journal, engine)


def status_report(journal: Journal, limit: int = 20) -> dict[str, Any]:
    def ex_view(e: dict[str, Any]) -> dict[str, Any]:
        return {k: (show(v) if hasattr(v, "is_finite") else v) for k, v in e.items() if k in (
            "id", "trade_id", "account_id", "native_symbol", "direction", "leverage", "state",
            "entry_outcome", "reason_code", "requested_margin", "target_notional", "limit_price",
            "planned_qty", "filled_qty", "avg_fill", "filled_notional", "actual_margin",
            "unused_allocation", "entry_fees", "exit_fees", "risk_at_stop", "risk_pct_of_margin",
            "pnl_at_stop", "protected_qty", "deadline_at", "close_reason", "realized_pnl", "net_pnl")}

    return {
        "phase": "1-simulation-only",
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


def write_status(rt: Runtime) -> Path:
    p = rt.config.data_dir / "status.json"
    rep = status_report(rt.journal)
    rep["generated_at"] = iso(rt.clock.now())
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)
    return p


def run_worker(cfg: Config, max_cycles: int | None = None, config_path: Path | None = None) -> int:
    """Foreground worker: single process (lock), startup reconcile, then periodic cycles."""
    from .config import ConfigError, load_config
    with ProcessLock(cfg.lock_path):
        rt = build_runtime(cfg)
        mtime = config_path.stat().st_mtime if config_path else None
        n = 0
        try:
            rt.engine.startup_reconcile()
            while max_cycles is None or n < max_cycles:
                if config_path and config_path.stat().st_mtime != mtime:
                    mtime = config_path.stat().st_mtime
                    try:
                        rt.engine.apply_config(load_config(config_path))
                        rt.config = rt.engine.cfg
                        rt.engine.alert("INFO", "CONFIG_RELOADED", "configuration reloaded")
                    except ConfigError as e:
                        rt.engine.alert("ERROR", "CONFIG_RELOAD_REJECTED", str(e))
                try:
                    rt.engine.run_cycle()
                except Exception:  # keep the worker alive; exits/protection retried next cycle
                    log.exception("cycle failed; continuing")
                write_status(rt)
                n += 1
                if max_cycles is None or n < max_cycles:
                    time.sleep(rt.config.poll_interval_seconds)
        except KeyboardInterrupt:
            log.info("worker stopped by operator")
        finally:
            rt.close()
    return n
