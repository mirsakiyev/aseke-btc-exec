"""Bounded soak runner (real-time) and accelerated deterministic stress.

Both run the real engine against the persistent simulator in a NEW isolated runtime root,
publish synthetic SIMULATION signals (HMAC-signed when the config's auth profile is ``strict``,
unsigned fixtures under ``simulation-compat-unsigned``), drive a seeded random-walk market, and
check lifecycle invariants after EVERY cycle. This is a CLEAN soak: it injects no venue faults
and performs no restarts (those are exercised separately by tests/scenarios).

Evidence files in the runtime root (never overwritten - the root must be new):
* ``soak-identity.json``   - written at start: runtime/source/config identity, parameters, auth.
* ``soak-progress.json``   - atomically replaced every ``progress_every_seconds`` of wall time.
* ``soak-checkpoints.jsonl`` - append-only history of the same snapshots (durable progress and
  invariant history: cumulative ``cycles_checked`` and ``violations_total`` per interval).
* ``soak-report.json``     - final report; ``status`` is COMPLETED only when the requested
  duration/cycle count was reached. Ctrl+C produces an INTERRUPTED partial report.

The report records MEASUREMENTS (elapsed wall time vs injected simulation time, latency,
CPU time, working set, database sizes, rejections, alerts, incidents, protection intervals,
storage checks). It does not invent pass thresholds; see docs/runbook.md for the project's
acceptance categories. Accelerated mode advances an injected clock and is NOT a real-time soak.
"""

from __future__ import annotations

import json
import os
import random
import statistics
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .auth import build_envelope, load_secret
from .clock import ManualClock, SystemClock, iso
from .config import load_config
from .engine import TERMINAL
from .examples import build_open
from .inbox import publish_event, signal_filename
from .journal import connect_readonly
from .lock import ProcessLock
from .num import D, floor_to_step, s
from .ops import process_memory_bytes, runtime_has_state, runtime_info
from .runtime import build_runtime, write_heartbeat
from .runtime_policy import require_operational

SOAK_FORMAT = "aseke.exec.soak-report/2"

#: Rejections the workload produces BY DESIGN (each is still counted and reported).
EXPECTED_REJECTIONS = {
    "OVERLAPPING_TRADE": "a new OPEN is published every N cycles while the previous trade on that "
                         "account may still be open (one active trade per account/instrument)",
}


def _dbsize(p: Path) -> int:
    return sum(x.stat().st_size for x in (p, p.with_name(p.name + "-wal")) if x.exists())


def _atomic_json(p: Path, data: dict[str, Any]) -> None:
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, p)


def _append_jsonl(p: Path, data: dict[str, Any]) -> None:
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(data, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def storage_checks(db: Path) -> dict[str, Any]:
    """PRAGMA integrity_check + foreign_key_check through a read-only connection. These detect
    structural corruption; they do not prove correct execution accounting."""
    if not db.exists():
        return {"exists": False}
    c = connect_readonly(db)
    try:
        ic = [r[0] for r in c.execute("PRAGMA integrity_check").fetchall()]
        fk = c.execute("PRAGMA foreign_key_check").fetchall()
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        c.close()
    return {"exists": True, "integrity_check": "ok" if ic == ["ok"] else ic[:20],
            "foreign_key_violations": len(fk), "journal_mode": mode}


def check_invariants(rt: Any) -> list[str]:
    j, sim, cfg = rt.journal, rt.sim, rt.config
    out: list[str] = []
    active = j.active_executions(TERMINAL)
    for aid, acct in cfg.accounts.items():
        for sym in acct.instruments.values():
            pos = sim.position(acct.venue, aid, sym)
            mine = [e for e in active if e["account_id"] == aid and e["native_symbol"] == sym]
            if len(mine) > 1:
                out.append(f"{aid}/{sym}: {len(mine)} active executions (overlap)")
            if pos["qty"] > 0:
                if not mine:
                    out.append(f"{aid}/{sym}: venue exposure {pos['qty']} without an active execution")
                    continue
                e = mine[0]
                if pos["side"] != e["direction"]:
                    out.append(f"{aid}/{sym}: position side {pos['side']} != {e['direction']} (REVERSAL)")
                working = sim.open_orders(acct.venue, aid, sym)
                stops = [o for o in working if o["kind"] == "STOP_MARKET"]
                covered = any(D(o["qty"]) >= pos["qty"] for o in stops)
                if e["state"] == "OPEN_PROTECTED" and not covered:
                    out.append(f"{aid}/{sym}: OPEN_PROTECTED but no venue stop covers {pos['qty']}")
                if e["stop_coverage"] == "VERIFIED" and not covered:
                    out.append(f"{aid}/{sym}: stop_coverage VERIFIED but the venue holds no covering stop")
                if len(stops) > 1:
                    out.append(f"{aid}/{sym}: {len(stops)} live stops at end of cycle (churn/duplicate)")
                tps = [o for o in working if o["kind"] == "TAKE_PROFIT_MARKET"]
                if sum((D(o["qty"]) for o in tps), Decimal(0)) > pos["qty"]:
                    out.append(f"{aid}/{sym}: working TP qty exceeds the position")
                if e["state"] == "CLOSING" and not covered and not e["unprotected_since"]:
                    out.append(f"{aid}/{sym}: CLOSING without covering stop and no unprotected marker")
            # orphan working exits for an account with no active execution
            if not mine and sim.open_orders(acct.venue, aid, sym):
                out.append(f"{aid}/{sym}: working orders while flat with no active execution (orphan)")
    for e in j.q("SELECT id, filled_qty FROM executions WHERE state='CLOSED'"):
        q = sum((f["qty"] for f in j.fills_for(e["id"], ("STOP", "TP", "CLOSE"))), Decimal(0))
        if q != e["filled_qty"]:
            out.append(f"execution {e['id']}: exit qty {q} != entry qty {e['filled_qty']}")
    return out


def _lat(lat: list[float]) -> dict[str, Any]:
    if not lat:
        return {"p50": None, "p95": None, "p99": None, "max": None, "first_10_mean": None,
                "last_10_mean": None}
    srt = sorted(lat)
    return {"p50": round(statistics.median(lat), 3), "p95": round(srt[int(0.95 * (len(srt) - 1))], 3),
            "p99": round(srt[int(0.99 * (len(srt) - 1))], 3), "max": round(srt[-1], 3),
            "first_10_mean": round(statistics.mean(lat[:10]), 3),
            "last_10_mean": round(statistics.mean(lat[-10:]), 3)}


def _journal_summary(j: Any) -> dict[str, Any]:
    states = {r["state"]: r["n"] for r in j.q("SELECT state, COUNT(*) AS n FROM executions GROUP BY state")}
    rej = {r["code"] or "?": r["n"] for r in j.q(
        "SELECT reason_code AS code, COUNT(*) AS n FROM executions WHERE state='REJECTED' "
        "GROUP BY reason_code ORDER BY n DESC")}
    ev_rej = {r["code"] or "?": r["n"] for r in j.q(
        "SELECT process_code AS code, COUNT(*) AS n FROM events WHERE process_status='REJECTED' "
        "GROUP BY process_code ORDER BY n DESC")}
    alerts = {f'{r["level"]}:{r["code"]}': r["n"] for r in j.q(
        "SELECT level, code, COUNT(*) AS n FROM alerts GROUP BY level, code ORDER BY n DESC")}
    incidents = [dict(r) for r in j.q(
        "SELECT id, kind, status, account_id, created_at, resolved_at FROM incidents ORDER BY id")]
    unprot = [D(r["m"]) for r in j.q(
        "SELECT max_unprotected_seconds AS m FROM executions WHERE max_unprotected_seconds IS NOT NULL")]
    ingest = {r["outcome"]: r["n"] for r in j.q(
        "SELECT outcome, COUNT(*) AS n FROM ingest_log GROUP BY outcome")}
    return {
        "execution_states": states,
        "rejections": {"expected": {k: v for k, v in rej.items() if k in EXPECTED_REJECTIONS},
                       "unexpected_review": {k: v for k, v in rej.items() if k not in EXPECTED_REJECTIONS},
                       "event_rejections": ev_rej, "expected_reason": EXPECTED_REJECTIONS},
        "ingest_outcomes": ingest,
        "alerts_by_level_code": alerts,
        "critical_alerts": sum(v for k, v in alerts.items() if k.startswith("CRITICAL:")),
        "incidents": incidents,
        "open_incidents": len(j.open_incidents()),
        "protection": {"executions_with_measured_unprotected_interval": len(unprot),
                       "max_unprotected_seconds": str(max(unprot)) if unprot else None,
                       "note": "entry-to-verified-stop interval is MEASURED, not eliminated"},
    }


def run_soak(config_path: str | Path, data_dir: str | Path, *, duration_seconds: float | None = None,
             cycles: int | None = None, accelerated: bool = False, poll_seconds: float = 0.5,
             trade_every_cycles: int = 8, seed: int = 7, max_holding_seconds: int = 30,
             progress_every_seconds: float = 60.0) -> dict[str, Any]:
    if (duration_seconds is None) == (cycles is None):
        raise ValueError("specify exactly one of duration_seconds / cycles")
    require_operational("soak")
    data_dir = Path(data_dir).resolve()
    if runtime_has_state(data_dir):
        raise FileExistsError(f"soak requires a NEW empty runtime root; {data_dir} is not empty")
    cfg = load_config(config_path, data_dir=data_dir)
    signer: tuple[str, bytes, str] | None = None
    if cfg.auth.mode == "strict":
        kid = cfg.auth.signer_key_id
        spec = cfg.auth.keys.get(kid) if kid else None
        secret = load_secret(spec) if spec else None
        if spec is None or secret is None:
            raise ValueError("strict soak needs the configured auth.signer key: set its env var "
                             f"({getattr(spec, 'secret_env', None)}) - check with `auth check`")
        signer = (kid, secret, spec.producer)  # type: ignore[assignment]
        auth_label = f"strict-signed (aseke.exec.envelope/1 HMAC-SHA256, key_id {kid})"
    elif cfg.auth.mode == "simulation-compat-unsigned":
        auth_label = "simulation-compat-unsigned (UNSIGNED fixtures; authentication NOT exercised)"
    else:  # pragma: no cover - config validation refuses other modes
        raise ValueError(f"unsupported auth mode {cfg.auth.mode}")
    clock = ManualClock(datetime.now(timezone.utc)) if accelerated else SystemClock()
    rng = random.Random(seed)
    lat: list[float] = []
    violations: list[str] = []
    violations_total = 0
    mem: list[int] = []
    wall0 = time.monotonic()
    cpu0 = time.process_time()
    started = iso(SystemClock().now())
    status = "RUNNING"
    data_dir.mkdir(parents=True, exist_ok=True)
    identity = {"format": SOAK_FORMAT, "started_at_wall": started, "auth": auth_label,
                "workload": "clean (no deliberate venue faults, no restarts)",
                "parameters": {"duration_seconds": duration_seconds, "cycles": cycles,
                               "accelerated": accelerated, "poll_seconds": poll_seconds,
                               "trade_every_cycles": trade_every_cycles, "seed": seed,
                               "max_holding_seconds": max_holding_seconds,
                               "progress_every_seconds": progress_every_seconds},
                "runtime": runtime_info(cfg)}
    _atomic_json(data_dir / "soak-identity.json", identity)
    report: dict[str, Any] = {}
    n = opened = 0
    with ProcessLock(cfg.lock_path):
        rt = build_runtime(cfg, clock)
        try:
            price = cfg.sim.initial_price
            j0, s0 = _dbsize(cfg.journal_path), _dbsize(cfg.sim.db_path)
            rt.engine.startup_reconcile()
            next_progress = time.monotonic() + progress_every_seconds

            def snapshot(state: str) -> dict[str, Any]:
                return {"ts_wall": iso(SystemClock().now()), "status": state,
                        "elapsed_wall_seconds": round(time.monotonic() - wall0, 3),
                        "simulated_seconds": round(n * poll_seconds, 3) if accelerated else None,
                        "cycles_checked": n, "signals_published": opened,
                        "violations_total": violations_total,
                        "cpu_seconds": round(time.process_time() - cpu0, 3),
                        "working_set_bytes": mem[-1] if mem else None,
                        "latency_ms_so_far": _lat(lat),
                        "open_incidents": len(rt.journal.open_incidents()),
                        "journal_bytes": _dbsize(cfg.journal_path)}

            try:
                while True:
                    if cycles is not None and n >= cycles:
                        break
                    if duration_seconds is not None and time.monotonic() - wall0 >= duration_seconds:
                        break
                    # seeded market random walk with occasional jumps that hit stops / targets
                    step = Decimal(rng.choice([-40, -25, -10, 0, 10, 25, 40]))
                    if rng.random() < 0.06:
                        step = Decimal(rng.choice([-900, 900, 1600, -1600]))
                    price = max(Decimal("1000"), floor_to_step(price + step, Decimal("0.1")))
                    for v in cfg.sim.venues.values():
                        for sym in v.instruments:
                            rt.sim.set_simple_market(v.name, sym, price, depth_btc="1")
                    if n % trade_every_cycles == 0:
                        long_ = rng.random() < 0.5
                        ref = price
                        kw = {"producer": signer[2]} if signer else {}
                        ev = build_open(clock.now(), f"soak-{n:06d}", direction="LONG" if long_ else "SHORT",
                                        reference_entry=s(ref),
                                        stop=s(ref - 500 if long_ else ref + 500),
                                        take_profits=[(s(ref + 800 if long_ else ref - 800), "0.5"),
                                                      (s(ref + 1500 if long_ else ref - 1500), "0.5")],
                                        holding={"max_holding_seconds": max_holding_seconds}, **kw)
                        if signer:
                            env = build_envelope(ev, key=signer[1], key_id=signer[0],
                                                 audience=cfg.auth.audience, issued_at=clock.now())
                            publish_event(cfg.inbox_dir, env, filename=signal_filename(ev))
                        else:
                            publish_event(cfg.inbox_dir, ev)
                        opened += 1
                    t0 = time.perf_counter()
                    rt.engine.run_cycle()
                    lat.append((time.perf_counter() - t0) * 1000)
                    write_heartbeat(rt)
                    found = check_invariants(rt)
                    violations_total += len(found)
                    for v in found:
                        if len(violations) < 200:
                            violations.append(f"cycle {n}: {v}")
                    m = process_memory_bytes()
                    if m:
                        mem.append(m)
                    n += 1
                    if time.monotonic() >= next_progress:
                        snap = snapshot("RUNNING")
                        _atomic_json(data_dir / "soak-progress.json", snap)
                        _append_jsonl(data_dir / "soak-checkpoints.jsonl", snap)
                        next_progress = time.monotonic() + progress_every_seconds
                    if accelerated:
                        clock.advance(poll_seconds)
                    else:
                        time.sleep(poll_seconds)
                status = "COMPLETED"
            except KeyboardInterrupt:
                status = "INTERRUPTED"
            final_snap = snapshot(status)
            _append_jsonl(data_dir / "soak-checkpoints.jsonl", final_snap)
            _atomic_json(data_dir / "soak-progress.json", final_snap)
            summary = _journal_summary(rt.journal)
            report = {
                "format": SOAK_FORMAT, "status": status,
                "completed": status == "COMPLETED",
                "mode": "accelerated (injected clock; NOT a real-time soak)" if accelerated else "real-time",
                "auth": auth_label, "workload": identity["workload"],
                "started_at_wall": started, "finished_at_wall": iso(SystemClock().now()),
                "elapsed_wall_seconds": round(time.monotonic() - wall0, 3),
                "requested": {"duration_seconds": duration_seconds, "cycles": cycles},
                "simulated_seconds": round(n * poll_seconds, 3) if accelerated else None,
                "cycles": n, "cycles_checked_for_invariants": n, "signals_published": opened, "seed": seed,
                **summary,
                "latency_ms": _lat(lat),
                "cpu_seconds": round(time.process_time() - cpu0, 3),
                "cpu_ms_per_cycle_mean": round((time.process_time() - cpu0) * 1000 / n, 3) if n else None,
                "working_set_bytes": {"first": mem[0] if mem else None, "last": mem[-1] if mem else None,
                                      "max": max(mem) if mem else None},
                "db_bytes": {"journal_start": j0, "journal_end": _dbsize(cfg.journal_path),
                             "sim_start": s0, "sim_end": _dbsize(cfg.sim.db_path)},
                "invariant_violations_total": violations_total,
                "invariant_violations": violations,
                "data_dir": str(data_dir),
                "runtime_policy": identity["runtime"].get("runtime_policy"),
                "evidence_files": ["soak-identity.json", "soak-progress.json", "soak-checkpoints.jsonl",
                                   "soak-report.json", "alerts.jsonl", "journal.db", "sim_exchange.db"],
            }
        finally:
            rt.close()
        # maintenance rule: still holding the worker lock, no writer is open
        report["storage"] = {"journal": storage_checks(cfg.journal_path),
                             "simulator": storage_checks(cfg.sim.db_path)}
    _atomic_json(data_dir / "soak-report.json", report)
    return report
