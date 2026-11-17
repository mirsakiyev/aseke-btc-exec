"""Real process boundaries: abrupt worker termination (os._exit at controlled crash points)
and restart against the SAME journal + independently persisted simulator; competing worker
starts; stale-lock recovery. Only subprocesses created here are ever terminated.
These are process-crash tests - NOT evidence of host power-loss safety."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

from aseke_exec.clock import SystemClock, parse_utc
from aseke_exec.examples import build_amend, build_close, build_open
from aseke_exec.faults import ENV, EXIT_CODE
from aseke_exec.inbox import publish_event
from aseke_exec.journal import connect_readonly
from aseke_exec.sim import SimExchange

ROOT = Path(__file__).resolve().parents[1]
A, B = "sim_alpha_a1", "sim_beta_b1"
VEN = {A: "sim_alpha", B: "sim_beta"}
SYMS = {A: "BTCUSDT-ALPHA", B: "BTCUSDT-BETA"}


class RT:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.cfg = tmp / "sim.toml"
        shutil.copy(ROOT / "config/sim.toml", self.cfg)
        self.root = tmp / "rt"
        assert self.cli("init-runtime").returncode == 0

    def cli(self, *args, crash=None, timeout=90):
        env = {k: v for k, v in os.environ.items() if k != ENV}
        if crash:
            env[ENV] = crash
        return subprocess.run([sys.executable, "-m", "aseke_exec", "--config", str(self.cfg),
                               "--data-dir", str(self.root), *args], capture_output=True, text=True,
                              env=env, timeout=timeout, cwd=str(ROOT))

    def popen(self, *args):
        env = {k: v for k, v in os.environ.items() if k != ENV}
        return subprocess.Popen([sys.executable, "-m", "aseke_exec", "--config", str(self.cfg),
                                 "--data-dir", str(self.root), *args], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env, cwd=str(ROOT))

    def publish(self, ev):
        return publish_event(self.root / "inbox", ev)

    def q(self, sql, args=()):
        # product read-only opener: bounded retry of the transient SQLITE_IOERR_TRUNCATE that
        # SQLite 3.53.x can return on the first read after a WAL writer was killed (Phase 2B)
        c = connect_readonly(self.root / "journal.db")
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(sql, args)]
        finally:
            c.close()

    def ex(self, trade, acct):
        r = self.q("SELECT * FROM executions WHERE trade_id=? AND account_id=?", (trade, acct))
        return r[0] if r else None

    def sim(self):  # only while no worker runs (single writer)
        return SimExchange(self.root / "sim_exchange.db", SystemClock())

    def venue(self, acct, kind=None, status=None):
        s = self.sim()
        try:
            return [dict(o) for o in s.all_orders(VEN[acct], acct)
                    if (kind is None or o["kind"] == kind) and (status is None or o["status"] == status)]
        finally:
            s.close()

    def pos(self, acct):
        s = self.sim()
        try:
            return s.position(VEN[acct], acct, SYMS[acct])
        finally:
            s.close()

    def venue_fills(self, acct, cid):
        s = self.sim()
        try:
            return [dict(f) for f in s.conn.execute(
                "SELECT * FROM fills WHERE account=? AND client_order_id=?", (acct, cid))]
        finally:
            s.close()


@pytest.fixture
def rt(tmp_path):
    return RT(tmp_path)


def _now():
    return SystemClock().now()


def test_crash_after_durable_ingestion_before_archive(rt):
    p = rt.publish(build_open(_now(), "c-ing"))
    r = rt.cli("cycle", crash="after_ingest_commit")
    assert r.returncode == EXIT_CODE
    assert p.exists(), "file not archived before the crash"
    assert rt.q("SELECT process_status FROM events")[0]["process_status"] == "PENDING"
    assert not rt.venue(A) and not rt.venue(B), "no external action before processing"
    assert rt.cli("cycle").returncode == 0
    assert not p.exists()
    assert rt.q("SELECT outcome FROM ingest_log WHERE outcome='DUPLICATE'"), "re-scan = duplicate"
    for a in (A, B):
        assert rt.ex("c-ing", a)["state"] == "OPEN_PROTECTED"
        assert len(rt.venue(a, "ENTRY_LIMIT_IOC")) == 1


def test_crash_after_intent_before_submission_never_submits_late(rt):
    rt.publish(build_open(_now(), "c-int"))
    assert rt.cli("cycle", crash="after_intent:ENTRY").returncode == EXIT_CODE
    o = rt.q("SELECT status FROM orders WHERE account_id=? AND leg='ENTRY'", (A,))
    assert o and o[0]["status"] == "INTENT" and not rt.venue(A)
    assert rt.cli("cycle").returncode == 0
    assert rt.cli("cycle").returncode == 0
    a, b = rt.ex("c-int", A), rt.ex("c-int", B)
    assert a["state"] == "ENTRY_NOT_ACCEPTED", a["state"]
    assert not rt.venue(A) and rt.pos(A)["qty"] == 0, "no late/duplicate entry for alpha"
    assert b["state"] == "OPEN_PROTECTED", "interrupted OPEN resumed for the un-routed account"
    rec = rt.q("SELECT ts FROM reconciliations WHERE account_id=? AND kind='STARTUP' ORDER BY id", (B,))
    assert parse_utc(rec[0]["ts"]) <= parse_utc(b["created_at"]), "reconciled before new entry"


def test_crash_after_venue_fill_before_local_ack(rt):
    rt.publish(build_open(_now(), "c-ack"))
    assert rt.cli("cycle", crash="after_ack:ENTRY").returncode == EXIT_CODE
    assert rt.pos(A)["qty"] == 7, "venue filled while the worker died"
    assert not rt.q("SELECT id FROM fills")
    s = rt.sim()  # market keeps moving while the worker is down
    try:
        for a in (A, B):
            s.set_simple_market(VEN[a], SYMS[a], "64800")
    finally:
        s.close()
    assert rt.cli("cycle").returncode == 0
    a = rt.ex("c-ack", A)
    assert a["state"] == "OPEN_PROTECTED" and Decimal(a["filled_qty"]) == 7
    assert len(rt.venue(A, "ENTRY_LIMIT_IOC")) == 1, "no duplicate entry"
    cid = rt.q("SELECT client_order_id FROM orders WHERE account_id=? AND leg='ENTRY'", (A,))[0]["client_order_id"]
    venue_fee = sum(Decimal(f["fee"]) for f in rt.venue_fills(A, cid))
    assert Decimal(a["entry_fees"]) == venue_fee > 0
    # beta was never routed before the crash; resumed routing re-runs FULL preflight against the
    # market that moved during the outage -> correctly refused as adverse (no stale entry).
    b = rt.ex("c-ack", B)
    assert b["state"] == "REJECTED" and b["reason_code"] == "ENTRY_PRICE_ADVERSE"
    assert not rt.venue(B, "ENTRY_LIMIT_IOC")


def test_crash_during_partial_fill_protection(rt):
    s = rt.sim()
    try:
        s.set_market("sim_alpha", SYMS[A], asks=[("64500.1", "3"), ("64700.0", "1000")])
    finally:
        s.close()
    rt.publish(build_open(_now(), "c-pfp"))
    assert rt.cli("cycle", crash="after_ack:STOP").returncode == EXIT_CODE
    assert len(rt.venue(A, "STOP_MARKET", "OPEN")) == 1, "stop accepted on venue before crash"
    assert rt.cli("cycle").returncode == 0
    a = rt.ex("c-pfp", A)
    assert a["entry_outcome"] == "PARTIAL_FILL" and Decimal(a["protected_qty"]) == 3
    stops = rt.venue(A, "STOP_MARKET")
    assert len(stops) == 1 and Decimal(stops[0]["qty"]) == 3, "no duplicate protective order"
    assert len(rt.venue(A, "ENTRY_LIMIT_IOC")) == 1, "partial fill never topped up"


def test_crash_during_protection_amendment(rt):
    rt.publish(build_open(_now(), "c-amd"))
    assert rt.cli("cycle").returncode == 0
    rt.publish(build_amend(_now(), "c-amd", 2, stop="64100.10", take_profits=[("66000.10", "1")]))
    assert rt.cli("cycle", crash="after_ack:STOP:v2").returncode == EXIT_CODE
    assert len(rt.venue(A, "STOP_MARKET", "OPEN")) == 2, "old and new stop briefly overlap (reduce-only)"
    assert rt.cli("cycle").returncode == 0
    for acct in (A, B):
        opens = rt.venue(acct, "STOP_MARKET", "OPEN")
        assert len(opens) == 1 and Decimal(opens[0]["trigger_price"]) == Decimal("64100.10")
        assert json.loads(rt.ex("c-amd", acct)["exit_plan"])["version"] == 2
    assert len(rt.venue(A, "STOP_MARKET")) == 2, "v2 stop not duplicated after restart"


@pytest.mark.parametrize("point", ["after_ack:CLOSE", "before_close_accounting"])
def test_crash_after_close_fill_before_local_accounting(rt, point):
    rt.publish(build_open(_now(), "c-cls"))
    assert rt.cli("cycle").returncode == 0
    rt.publish(build_close(_now(), "c-cls", 2))
    assert rt.cli("cycle", crash=point).returncode == EXIT_CODE
    assert rt.pos(A)["qty"] == 0, "venue already flat"
    assert rt.ex("c-cls", A)["state"] != "CLOSED"
    assert rt.cli("cycle").returncode == 0
    a = rt.ex("c-cls", A)
    assert a["state"] == "CLOSED" and a["close_reason"] == "LAB_CLOSE"
    assert len(rt.venue(A, "CLOSE_MARKET")) == 1, "close not resent"
    p = rt.pos(A)
    assert p["qty"] == 0 and p["side"] is None, "no reversal"
    cid = rt.q("SELECT client_order_id FROM orders WHERE account_id=? AND leg='CLOSE'", (A,))[0]["client_order_id"]
    vf = rt.venue_fills(A, cid)
    assert Decimal(a["exit_fees"]) == sum(Decimal(f["fee"]) for f in vf)
    assert len(rt.q("SELECT id FROM fills WHERE execution_id=? AND leg='CLOSE'", (a["id"],))) == len(vf)
    assert not rt.venue(A, status="OPEN"), "orphaned exits cancelled"


def test_close_before_open_tombstone_survives_restart(rt):
    rt.publish(build_close(_now(), "c-tomb", 2))
    assert rt.cli("cycle").returncode == 0
    rt.publish(build_open(_now(), "c-tomb"))
    assert rt.cli("cycle").returncode == 0  # a new process
    assert rt.q("SELECT process_code FROM events WHERE event_type='OPEN'")[0]["process_code"] == "TRADE_TOMBSTONED"
    assert not rt.q("SELECT id FROM executions") and not rt.venue(A)


def test_competing_workers_operator_queue_and_stale_lock(rt):
    w = rt.popen("run", "--poll-seconds", "0.2", "--max-cycles", "100000")
    try:
        hb = rt.root / "heartbeat.json"
        t0 = time.monotonic()
        while not hb.exists() and time.monotonic() - t0 < 30:
            time.sleep(0.1)
        assert hb.exists(), "worker did not start"
        r2 = rt.cli("cycle")
        r3 = rt.cli("run", "--max-cycles", "1")
        assert r2.returncode == 3 and "refused" in r2.stdout
        assert r3.returncode == 3, "second worker refused"
        assert (rt.root / "worker.lock").exists(), "active lock never deleted"
        rp = rt.cli("pause", "--reason", "test")
        assert rp.returncode == 0 and rp.stdout.startswith("QUEUED"), rp.stdout
        t0 = time.monotonic()
        while list((rt.root / "control").glob("*.json")) and time.monotonic() - t0 < 20:
            time.sleep(0.1)
        assert not list((rt.root / "control").glob("*.json")), "running worker applied the request"
    finally:
        w.kill()  # terminate ONLY the worker this test started (abrupt: no cleanup)
        w.wait(timeout=30)
    assert rt.q("SELECT value FROM controls WHERE key='pause_new_entries'")[0]["value"] == "1"
    assert rt.q("SELECT outcome FROM operator_actions")[0]["outcome"] == "APPLIED"
    # stale lock: the file remains and even names a LIVE pid (this test process) but no OS lock
    # is held -> a new worker may start; PID existence alone never blocks or deletes a lock.
    (rt.root / "worker.lock").write_text(str(os.getpid()), encoding="utf-8")
    r4 = rt.cli("cycle")
    assert r4.returncode == 0, r4.stdout + r4.stderr
    assert (rt.root / "worker.lock").exists()


def test_health_reports_a_resident_worker_and_its_absence(rt):
    w = rt.popen("run", "--poll-seconds", "0.2", "--max-cycles", "100000")
    try:
        t0 = time.monotonic()
        while not (rt.root / "heartbeat.json").exists() and time.monotonic() - t0 < 30:
            time.sleep(0.1)
        time.sleep(0.5)
        rep = json.loads(rt.cli("health", "--json").stdout)
        assert rep["process"]["running"] is True and rep["process"]["pid_alive"] is True
        assert rep["process"]["heartbeat_kind"] == "worker"
    finally:
        w.kill()
        w.wait(timeout=30)
    rep = json.loads(rt.cli("health", "--json").stdout)
    assert rep["process"]["running"] is False and rep["process"]["pid_alive"] is False
