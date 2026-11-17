"""Bounded soak runner (real-time) and accelerated deterministic stress.

Both run the real engine against the persistent simulator in a NEW isolated runtime root,
publish synthetic SIMULATION signals, drive a seeded random-walk market, and check lifecycle
invariants after every cycle. The report records what was MEASURED (elapsed wall time,
cycle latency, working-set memory, database sizes, counts, violations); it does not invent
pass thresholds. Accelerated mode advances an injected clock and is NOT a real-time soak.
"""

from __future__ import annotations

import json
import random
import statistics
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .clock import ManualClock, SystemClock, iso
from .config import load_config
from .engine import TERMINAL
from .examples import build_open
from .inbox import publish_event
from .lock import ProcessLock
from .num import D, floor_to_step, s
from .ops import process_memory_bytes, runtime_has_state
from .runtime import build_runtime, write_heartbeat


def _dbsize(p: Path) -> int:
    return sum(x.stat().st_size for x in (p, p.with_name(p.name + "-wal")) if x.exists())


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


def run_soak(config_path: str | Path, data_dir: str | Path, *, duration_seconds: float | None = None,
             cycles: int | None = None, accelerated: bool = False, poll_seconds: float = 0.5,
             trade_every_cycles: int = 8, seed: int = 7, max_holding_seconds: int = 30) -> dict[str, Any]:
    if (duration_seconds is None) == (cycles is None):
        raise ValueError("specify exactly one of duration_seconds / cycles")
    data_dir = Path(data_dir).resolve()
    if runtime_has_state(data_dir):
        raise FileExistsError(f"soak requires a NEW empty runtime root; {data_dir} is not empty")
    cfg = load_config(config_path, data_dir=data_dir)
    if cfg.auth.mode != "simulation-compat-unsigned":
        raise ValueError("soak publishes unsigned synthetic fixtures; use the compatibility profile config")
    clock = ManualClock(datetime.now(timezone.utc)) if accelerated else SystemClock()
    rng = random.Random(seed)
    lat: list[float] = []
    violations: list[str] = []
    mem: list[int] = []
    wall0 = time.monotonic()
    started = iso(SystemClock().now())
    with ProcessLock(cfg.lock_path):
        rt = build_runtime(cfg, clock)
        try:
            price = cfg.sim.initial_price
            j0, s0 = _dbsize(cfg.journal_path), _dbsize(cfg.sim.db_path)
            rt.engine.startup_reconcile()
            n = opened = 0
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
                    ev = build_open(clock.now(), f"soak-{n:06d}", direction="LONG" if long_ else "SHORT",
                                    reference_entry=s(ref),
                                    stop=s(ref - 500 if long_ else ref + 500),
                                    take_profits=[(s(ref + 800 if long_ else ref - 800), "0.5"),
                                                  (s(ref + 1500 if long_ else ref - 1500), "0.5")],
                                    holding={"max_holding_seconds": max_holding_seconds})
                    publish_event(cfg.inbox_dir, ev)
                    opened += 1
                t0 = time.perf_counter()
                rt.engine.run_cycle()
                lat.append((time.perf_counter() - t0) * 1000)
                write_heartbeat(rt)
                for v in check_invariants(rt):
                    if len(violations) < 200:
                        violations.append(f"cycle {n}: {v}")
                m = process_memory_bytes()
                if m:
                    mem.append(m)
                n += 1
                if accelerated:
                    clock.advance(poll_seconds)
                else:
                    time.sleep(poll_seconds)
            j = rt.journal
            states = {r["state"]: r["n"] for r in j.q("SELECT state, COUNT(*) AS n FROM executions GROUP BY state")}
            report = {
                "mode": "accelerated (injected clock; NOT a real-time soak)" if accelerated else "real-time",
                "started_at_wall": started, "elapsed_wall_seconds": round(time.monotonic() - wall0, 3),
                "simulated_seconds": round(n * poll_seconds, 3) if accelerated else None,
                "cycles": n, "signals_published": opened, "seed": seed,
                "execution_states": states,
                "open_incidents": len(j.open_incidents()),
                "alerts_by_code": {r["code"]: r["n"] for r in j.q(
                    "SELECT code, COUNT(*) AS n FROM alerts GROUP BY code ORDER BY n DESC")},
                "latency_ms": {"p50": round(statistics.median(lat), 3) if lat else None,
                               "p95": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 3) if lat else None,
                               "max": round(max(lat), 3) if lat else None,
                               "first_10_mean": round(statistics.mean(lat[:10]), 3) if lat else None,
                               "last_10_mean": round(statistics.mean(lat[-10:]), 3) if lat else None},
                "working_set_bytes": {"first": mem[0] if mem else None, "last": mem[-1] if mem else None,
                                      "max": max(mem) if mem else None},
                "db_bytes": {"journal_start": j0, "journal_end": _dbsize(cfg.journal_path),
                             "sim_start": s0, "sim_end": _dbsize(cfg.sim.db_path)},
                "invariant_violations": violations,
                "data_dir": str(data_dir),
            }
        finally:
            rt.close()
    (data_dir / "soak-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
