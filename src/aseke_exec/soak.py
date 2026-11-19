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

Phase 2C (docs/phase2c-acceptance-report.md):
* the workload comes from :mod:`aseke_exec.soakgen` (same seeded walk; corrected exit levels) and
  every generated OPEN is validated by the production contract parser BEFORE it is signed and
  published. A generator failure is counted in ``workload.generator_errors``, never published and
  never counted as a published signal; it makes the CLI exit 1.
* ``funding_profile`` selects TEST-ONLY synthetic starting wallets for the NEW simulator
  (``FUNDING_PROFILES``); every account/risk policy is untouched and nothing replenishes wallets.
* the report adds signal/file reconciliation, per-profile expected vs unexpected rejections,
  starting/ending synthetic balances with ledger reconciliation, coverage per run quarter, and a
  ``workload_acceptance`` verdict. The CLEAN workload permits no invalid generated signals.
* the per-cycle exit-quantity check of CLOSED executions is incremental (each is checked once
  when it closes, and all are re-checked at the end) - the Phase 2B harness re-read every closed
  execution every cycle, so its own CPU cost grew with run length.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
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
from .inbox import publish_event, signal_filename
from .journal import connect_readonly
from .lock import ProcessLock
from .num import ZERO, D, floor_to_step, s
from .ops import process_memory_bytes, runtime_has_state, runtime_info
from .runtime import build_runtime, write_heartbeat
from .runtime_policy import require_operational
from .soakgen import (LEVEL_POLICY, MarketWalk, SoakWorkloadError, build_soak_open, common_tick,
                      exit_levels, validate_generated)

SOAK_FORMAT = "aseke.exec.soak-report/3"

_OVERLAP_WHY = ("a new OPEN is published every N cycles while the previous trade on that account "
                "may still be open (one active trade per account/instrument)")
#: Rejections the workload produces BY DESIGN under the default profile (still counted/reported).
EXPECTED_REJECTIONS = {"OVERLAPPING_TRADE": _OVERLAP_WHY}

#: TEST-ONLY synthetic starting wallets for the NEW simulator of a soak root. ``None`` keeps the
#: configured ``[sim.accounts.*].starting_balance``. Only wallets change: margin per trade,
#: leverage, isolated mode, tolerances, fees, instrument rules and hard limits are the config's.
FUNDING_PROFILES: dict[str, dict[str, Any]] = {
    "config": {"starting_balance": None, "expected_rejections": EXPECTED_REJECTIONS,
               "label": "as configured (synthetic wallets from the config file)"},
    "funded-test-100k": {
        "starting_balance": Decimal("100000.00"), "expected_rejections": EXPECTED_REJECTIONS,
        "label": "TEST-ONLY funded simulation: 100,000 synthetic USDT per account; not real "
                 "capital, not trading advice; no replenishment"},
    "low-capital-test-50": {
        "starting_balance": Decimal("50.00"),
        "expected_rejections": {"INSUFFICIENT_COLLATERAL": "negative test: 50 synthetic USDT is "
                                "below one trade's margin + buffer + fee reservation"},
        "label": "TEST-ONLY negative profile: 50 synthetic USDT per account (collateral refusal)"},
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


def check_invariants(rt: Any, checked_closed: set[int] | None = None) -> list[str]:
    """Venue/journal lifecycle invariants. With ``checked_closed`` the exit-quantity check runs
    only for CLOSED executions not yet in the set (they are terminal) and adds them; ``None``
    re-checks every CLOSED execution."""
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
        if checked_closed is not None:
            if e["id"] in checked_closed:
                continue
            checked_closed.add(e["id"])
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


def _journal_summary(j: Any, expected: dict[str, str] = EXPECTED_REJECTIONS) -> dict[str, Any]:
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
        "rejections": {"expected": {k: v for k, v in rej.items() if k in expected},
                       "unexpected_review": {k: v for k, v in rej.items() if k not in expected},
                       "event_rejections": ev_rej, "expected_reason": expected},
        "ingest_outcomes": ingest,
        "alerts_by_level_code": alerts,
        "critical_alerts": sum(v for k, v in alerts.items() if k.startswith("CRITICAL:")),
        "incidents": incidents,
        "open_incidents": len(j.open_incidents()),
        "protection": {"executions_with_measured_unprotected_interval": len(unprot),
                       "max_unprotected_seconds": str(max(unprot)) if unprot else None,
                       "note": "entry-to-verified-stop interval is MEASURED, not eliminated"},
    }


def _apply_funding(cfg: Any, profile: str) -> Any:
    if profile not in FUNDING_PROFILES:
        raise ValueError(f"unknown funding profile {profile!r}; choose one of {sorted(FUNDING_PROFILES)}")
    bal = FUNDING_PROFILES[profile]["starting_balance"]
    if bal is None:
        return cfg
    accts = {aid: dataclasses.replace(a, starting_balance=bal) for aid, a in cfg.sim.accounts.items()}
    return dataclasses.replace(cfg, sim=dataclasses.replace(cfg.sim, accounts=accts))


def _funding_identity(cfg: Any, configured: dict[str, Decimal], profile: str) -> dict[str, Any]:
    start = {aid: s(a.starting_balance) for aid, a in cfg.sim.accounts.items()}
    fp = hashlib.sha256(json.dumps({"config_source_hash": cfg.source_hash, "profile": profile,
                                    "starting_balances": start}, sort_keys=True).encode()).hexdigest()
    return {"profile": profile, "label": FUNDING_PROFILES[profile]["label"], "synthetic": True,
            "starting_balances": start,
            "configured_starting_balances": {k: s(v) for k, v in configured.items()},
            "replenishment": "none", "config_source_hash": cfg.source_hash,
            "effective_funding_fingerprint_sha256": fp,
            "expected_rejections": FUNDING_PROFILES[profile]["expected_rejections"]}


def _effective_policies(cfg: Any) -> dict[str, Any]:
    pol = {}
    for aid, a in cfg.accounts.items():
        pol[aid] = {k: (s(v) if isinstance(v, Decimal) else v) for k, v in (
            ("venue", a.venue), ("margin_currency", a.margin_currency),
            ("initial_margin_per_trade", a.initial_margin_per_trade),
            ("adverse_entry_tolerance", a.adverse_entry_tolerance),
            ("max_spread_fraction", a.max_spread_fraction),
            ("collateral_buffer_fraction", a.collateral_buffer_fraction),
            ("max_initial_margin_hard", a.max_initial_margin_hard),
            ("max_notional_hard", a.max_notional_hard), ("max_loss_at_stop", a.max_loss_at_stop))}
    return {"accounts": pol, "margin_mode": "ISOLATED (engine-enforced)",
            "leverage": "from signal (soak signals request 5x)", "entry": "IMMEDIATE_IOC",
            "venue_fee_rates": {v.name: s(v.taker_fee_rate) for v in cfg.sim.venues.values()},
            "instrument_ticks": {f"{v.name}/{sym}": s(r.tick_size)
                                 for v in cfg.sim.venues.values() for sym, r in v.instruments.items()}}


def _wallets(rt: Any) -> dict[str, dict[str, str]]:
    out = {}
    for aid, a in rt.config.sim.accounts.items():
        b = rt.sim.balance(a.venue, aid)
        out[aid] = {"wallet": s(b["wallet"]), "available": s(b["available"])}
    return out


def _balances(rt: Any) -> dict[str, Any]:
    """Synthetic ledger at the end: start + realized P&L + fees must equal the wallet."""
    out: dict[str, Any] = {}
    for aid, a in rt.config.sim.accounts.items():
        sums: dict[str, Decimal] = {}
        for r in rt.sim.conn.execute("SELECT kind, amount FROM ledger WHERE venue=? AND account=?",
                                     (a.venue, aid)):
            sums[r["kind"]] = sums.get(r["kind"], ZERO) + D(r["amount"])
        b = rt.sim.balance(a.venue, aid)
        expect = a.starting_balance + sum(sums.values(), ZERO)
        out[aid] = {"currency": b["currency"], "starting": s(a.starting_balance),
                    "ending_wallet": s(b["wallet"]), "ending_available": s(b["available"]),
                    "realized_pnl": s(sums.get("REALIZED_PNL", ZERO)), "fees": s(sums.get("FEE", ZERO)),
                    "other_ledger": {k: s(v) for k, v in sums.items() if k not in ("REALIZED_PNL", "FEE")},
                    "ledger_reconciles": expect == b["wallet"]}
    return out


def _coverage(j: Any, cycles: int, quarters: int = 4) -> list[dict[str, Any]]:
    """Execution activity per run quarter (by the cycle encoded in trade_id ``soak-NNNNNN``)."""
    span = max(cycles, 1)
    rows = [dict(r, q=min(quarters - 1, int(r["trade_id"].split("-")[1]) * quarters // span))
            for r in j.q("SELECT trade_id, account_id, state, reason_code FROM executions "
                         "WHERE trade_id LIKE 'soak-%'")]
    out = []
    for qi in range(quarters):
        # cycle c is in quarter floor(c*Q/span): the inverse is lo = ceil(qi*span/Q)
        lo, hi = -(-qi * span // quarters), -(-(qi + 1) * span // quarters) - 1
        acc: dict[str, Any] = {}
        for r in (x for x in rows if x["q"] == qi):
            a = acc.setdefault(r["account_id"], {"routed": 0, "closed": 0, "active": 0, "rejected": {}})
            a["routed"] += 1
            if r["state"] == "CLOSED":
                a["closed"] += 1
            elif r["state"] == "REJECTED":
                code = r["reason_code"] or "?"
                a["rejected"][code] = a["rejected"].get(code, 0) + 1
            elif r["state"] not in TERMINAL:
                a["active"] += 1
        out.append({"quarter": qi + 1, "cycles": [lo, hi], "accounts": acc})
    return out


def _files(d: Path) -> int:
    if not d.exists():
        return 0
    return sum(1 for p in d.iterdir() if p.is_file() and p.name.endswith(".json")
               and not p.name.endswith(".reason.json") and not p.name.startswith("."))


def _reconciliation(rt: Any, generated: int, gen_errors: int, published: int) -> dict[str, Any]:
    j, cfg = rt.journal, rt.config
    ingest = {r["outcome"]: r["n"] for r in j.q("SELECT outcome, COUNT(*) AS n FROM ingest_log GROUP BY outcome")}
    inbox_left = _files(cfg.inbox_dir)
    archive = _files(cfg.inbox_dir.parent / "archive")
    quarantine = _files(cfg.inbox_dir.parent / "quarantine")
    opens = j.q1("SELECT COUNT(*) AS n FROM events WHERE event_type='OPEN'")["n"]
    routed = {sid: len(a) for sid, a in cfg.routes.items()}
    per_open = max(routed.values()) if routed else 0
    execs = j.q1("SELECT COUNT(*) AS n FROM executions")["n"]
    j_orders = j.q1("SELECT COUNT(*) AS n FROM orders")["n"]
    j_fills = j.q1("SELECT COUNT(*) AS n FROM fills")["n"]
    s_orders = rt.sim.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    s_fills = rt.sim.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    s_pos = sum(1 for r in rt.sim.conn.execute("SELECT qty FROM positions") if D(r[0]) > 0)
    active = len(j.active_executions(TERMINAL))
    checks = {
        "generated == published + generator_errors": generated == published + gen_errors,
        "published == ingested outcomes + remaining inbox": published == sum(ingest.values()) + inbox_left,
        "archive files == JOURNALED + DUPLICATE":
            archive == ingest.get("JOURNALED", 0) + ingest.get("DUPLICATE", 0),
        "quarantine files == INVALID + CONFLICT":
            quarantine == ingest.get("INVALID", 0) + ingest.get("CONFLICT", 0),
        "journal OPEN events == JOURNALED": opens == ingest.get("JOURNALED", 0),
        "executions == OPEN events x routed accounts": execs == opens * per_open,
        "journal orders == simulator orders": j_orders == s_orders,
        "journal fills == simulator fills": j_fills == s_fills,
        "simulator open positions <= active executions": s_pos <= active,
    }
    return {"signals": {"generated": generated, "generator_errors": gen_errors, "published": published,
                        "ingest_outcomes": ingest, "inbox_remaining": inbox_left,
                        "archive_files": archive, "quarantine_files": quarantine},
            "journal": {"open_events": opens, "routed_accounts_per_open": per_open, "executions": execs,
                        "orders": j_orders, "fills": j_fills, "active_executions": active},
            "simulator": {"orders": s_orders, "fills": s_fills, "open_positions": s_pos},
            "checks": checks, "all_reconcile": all(checks.values())}


def run_soak(config_path: str | Path, data_dir: str | Path, *, duration_seconds: float | None = None,
             cycles: int | None = None, accelerated: bool = False, poll_seconds: float = 0.5,
             trade_every_cycles: int = 8, seed: int = 7, max_holding_seconds: int = 30,
             progress_every_seconds: float = 60.0, funding_profile: str = "config") -> dict[str, Any]:
    if (duration_seconds is None) == (cycles is None):
        raise ValueError("specify exactly one of duration_seconds / cycles")
    require_operational("soak")
    if funding_profile not in FUNDING_PROFILES:
        raise ValueError(f"unknown funding profile {funding_profile!r}; "
                         f"choose one of {sorted(FUNDING_PROFILES)}")
    data_dir = Path(data_dir).resolve()
    if runtime_has_state(data_dir):
        raise FileExistsError(f"soak requires a NEW empty runtime root; {data_dir} is not empty")
    cfg0 = load_config(config_path, data_dir=data_dir)
    configured = {aid: a.starting_balance for aid, a in cfg0.sim.accounts.items()}
    cfg = _apply_funding(cfg0, funding_profile)
    expected = FUNDING_PROFILES[funding_profile]["expected_rejections"]
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
    ticks = [r.tick_size for v in cfg.sim.venues.values() for r in v.instruments.values()]
    grid = common_tick(ticks)
    clock = ManualClock(datetime.now(timezone.utc)) if accelerated else SystemClock()
    walk = MarketWalk(seed, cfg.sim.initial_price)
    lat: list[float] = []
    inv_ms: list[float] = []
    violations: list[str] = []
    violations_total = 0
    gen_errors: list[str] = []
    gen_error_total = 0
    scaled = 0
    checked_closed: set[int] = set()
    seen_msgs: set[str] = set()  # distinct violations already counted (final pass dedupe)
    mem: list[int] = []
    wall0 = time.monotonic()
    cpu0 = time.process_time()
    started = iso(SystemClock().now())
    status = "RUNNING"
    data_dir.mkdir(parents=True, exist_ok=True)
    funding = _funding_identity(cfg, configured, funding_profile)
    identity = {"format": SOAK_FORMAT, "started_at_wall": started, "auth": auth_label,
                "workload": "clean (no deliberate venue faults, no restarts, no invalid signals)",
                "workload_generator": {"level_policy": LEVEL_POLICY, "common_tick": s(grid),
                                       "validated_before_publish": "production contract parser + tick check"},
                "funding": funding, "effective_policies": _effective_policies(cfg),
                "parameters": {"duration_seconds": duration_seconds, "cycles": cycles,
                               "accelerated": accelerated, "poll_seconds": poll_seconds,
                               "trade_every_cycles": trade_every_cycles, "seed": seed,
                               "max_holding_seconds": max_holding_seconds,
                               "progress_every_seconds": progress_every_seconds,
                               "funding_profile": funding_profile},
                "runtime": runtime_info(cfg)}
    _atomic_json(data_dir / "soak-identity.json", identity)
    report: dict[str, Any] = {}
    n = opened = generated = 0
    with ProcessLock(cfg.lock_path):
        rt = build_runtime(cfg, clock)
        try:
            j0, s0 = _dbsize(cfg.journal_path), _dbsize(cfg.sim.db_path)
            rt.engine.startup_reconcile()
            next_progress = time.monotonic() + progress_every_seconds

            def snapshot(state: str) -> dict[str, Any]:
                return {"ts_wall": iso(SystemClock().now()), "status": state,
                        "elapsed_wall_seconds": round(time.monotonic() - wall0, 3),
                        "simulated_seconds": round(n * poll_seconds, 3) if accelerated else None,
                        "cycles_checked": n, "signals_generated": generated,
                        "signals_published": opened, "generator_errors": gen_error_total,
                        "violations_total": violations_total,
                        "cpu_seconds": round(time.process_time() - cpu0, 3),
                        "working_set_bytes": mem[-1] if mem else None,
                        "latency_ms_so_far": _lat(lat),
                        "open_incidents": len(rt.journal.open_incidents()),
                        "journal_bytes": _dbsize(cfg.journal_path), "wallets": _wallets(rt)}

            try:
                while True:
                    if cycles is not None and n >= cycles:
                        break
                    if duration_seconds is not None and time.monotonic() - wall0 >= duration_seconds:
                        break
                    # seeded market random walk with occasional jumps that hit stops / targets
                    price = walk.advance()
                    for v in cfg.sim.venues.values():
                        for sym in v.instruments:
                            rt.sim.set_simple_market(v.name, sym, price, depth_btc="1")
                    if n % trade_every_cycles == 0:
                        long_ = walk.draw_long()
                        generated += 1
                        try:
                            ref = floor_to_step(price, grid)
                            lv = exit_levels(ref, long_, grid)
                            ev = build_soak_open(clock.now(), f"soak-{n:06d}", ref, long_, lv,
                                                 max_holding_seconds=max_holding_seconds,
                                                 producer=signer[2] if signer else None)
                            validate_generated(ev, ticks)
                        except (SoakWorkloadError, ArithmeticError, ValueError, KeyError,
                                TypeError) as e:  # harness defect: counted, never published
                            gen_error_total += 1
                            if len(gen_errors) < 200:
                                gen_errors.append(f"cycle {n}: {type(e).__name__}: {e}")
                        else:
                            scaled += int(lv.scale < 1)
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
                    t1 = time.perf_counter()
                    found = check_invariants(rt, checked_closed)
                    inv_ms.append((time.perf_counter() - t1) * 1000)
                    violations_total += len(found)
                    seen_msgs.update(found)
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
            # final FULL invariant pass: every CLOSED execution is re-checked once more
            # (a violation already counted in-loop is not counted a second time)
            final_found = [v for v in check_invariants(rt, None) if v not in seen_msgs]
            violations_total += len(final_found)
            violations.extend(f"final: {v}" for v in final_found[:200])
            final_snap = snapshot(status)
            _append_jsonl(data_dir / "soak-checkpoints.jsonl", final_snap)
            _atomic_json(data_dir / "soak-progress.json", final_snap)
            summary = _journal_summary(rt.journal, expected)
            recon = _reconciliation(rt, generated, gen_error_total, opened)
            not_journaled = {k: v for k, v in summary["ingest_outcomes"].items() if k != "JOURNALED"}
            unexpected = dict(summary["rejections"]["unexpected_review"])
            unexpected.update({f"event:{k}": v for k, v in summary["rejections"]["event_rejections"].items()})
            acceptance: dict[str, Any] = {
                "generator_errors": gen_error_total, "ingest_not_journaled": not_journaled,
                "unexpected_rejections": unexpected, "reconciles": recon["all_reconcile"],
                "invariant_violations_total": violations_total,
                "workload_clean": gen_error_total == 0 and not not_journaled,
                "note": "CLEAN workload: every generated OPEN must be valid and journaled; an "
                        "unexpected rejection requires review and is never relabelled as expected"}
            acceptance["ok"] = bool(acceptance["workload_clean"] and not unexpected
                                    and recon["all_reconcile"] and violations_total == 0)
            report = {
                "format": SOAK_FORMAT, "status": status,
                "completed": status == "COMPLETED",
                "mode": "accelerated (injected clock; NOT a real-time soak)" if accelerated else "real-time",
                "auth": auth_label, "workload": identity["workload"],
                "funding": funding,
                "started_at_wall": started, "finished_at_wall": iso(SystemClock().now()),
                "elapsed_wall_seconds": round(time.monotonic() - wall0, 3),
                "requested": {"duration_seconds": duration_seconds, "cycles": cycles},
                "simulated_seconds": round(n * poll_seconds, 3) if accelerated else None,
                "cycles": n, "cycles_checked_for_invariants": n, "signals_published": opened, "seed": seed,
                "workload_generator": {**identity["workload_generator"], "signals_generated": generated,
                                       "signals_scaled": scaled, "generator_errors": gen_error_total,
                                       "generator_error_samples": gen_errors},
                **summary,
                "workload_acceptance": acceptance,
                "reconciliation": recon,
                "balances": _balances(rt),
                "coverage_by_quarter": _coverage(rt.journal, n),
                "latency_ms": _lat(lat),
                "invariant_check_ms": _lat(inv_ms),
                "cpu_seconds": round(time.process_time() - cpu0, 3),
                "cpu_ms_per_cycle_mean": round((time.process_time() - cpu0) * 1000 / n, 3) if n else None,
                "working_set_bytes": {"first": mem[0] if mem else None, "last": mem[-1] if mem else None,
                                      "max": max(mem) if mem else None},
                "db_bytes": {"journal_start": j0, "journal_end": _dbsize(cfg.journal_path),
                             "sim_start": s0, "sim_end": _dbsize(cfg.sim.db_path)},
                "invariant_violations_total": violations_total,
                "invariant_violations": violations,
                "data_dir": str(data_dir),
                "source": {k: identity["runtime"].get(k) for k in ("git_head", "source_fingerprint_sha256")},
                "runtime_policy": identity["runtime"].get("runtime_policy"),
                "scope_note": "simulation only: no strategy-profitability, liquidation, funding or "
                              "venue-fill validation; balances are synthetic",
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
