"""Phase 2A protection integrity, degraded states, sizing invariants and local-persistence
faults (in-process, deterministic ManualClock, isolated temp runtime roots)."""

import logging
import sqlite3
from decimal import Decimal

import pytest

from aseke_exec.contract import load_event_json
from aseke_exec.examples import build_open
from aseke_exec.reasons import Rejection
from aseke_exec.scenarios import A, B, Harness

D = Decimal
MTP = [("65000.10", "0.5"), ("66000.10", "0.5")]


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def mk(tmp_path):
    made = []

    def make(mutate=None, **start):
        h = Harness(tmp_path / f"h{len(made)}", mutate)
        made.append(h)
        h.start(**start)
        return h
    yield make
    for h in made:
        if h.journal is not None:
            h.journal.close()
        h.sim.close()


def tp_legs(h, trade, acct):
    return [(D(l["qty"]), D(l["executed_qty"]), D(l["remaining_qty"]))
            for l in h.ex(trade, acct)["exit_plan"]["legs"]]


def test_healthy_unchanged_position_generates_no_order_churn(mk):
    h = mk()
    h.publish(build_open(h.now(), "t-churn", take_profits=MTP))
    h.cycle()
    before_orders = [dict(o) for a in (A, B) for o in h.venue_orders(a)]
    before_subs = h.j.q1("SELECT COUNT(*) AS n FROM submissions")["n"]
    for _ in range(25):
        h.clock.advance(2)
        h.cycle()
    after_orders = [dict(o) for a in (A, B) for o in h.venue_orders(a)]
    assert [(o["client_order_id"], o["status"], o["qty"], o["trigger_price"]) for o in after_orders] == \
           [(o["client_order_id"], o["status"], o["qty"], o["trigger_price"]) for o in before_orders]
    assert h.j.q1("SELECT COUNT(*) AS n FROM submissions")["n"] == before_subs, "no venue writes"
    for a in (A, B):
        e = h.ex("t-churn", a)
        assert (e["stop_coverage"], e["tp_coverage"]) == ("VERIFIED", "COMPLETE")


def test_partial_tp_fill_is_accounted_per_leg_and_not_rearmed(mk):
    h = mk()
    h.sim.add_fault("sim_alpha", "trigger", "PARTIAL", account=A, param="0.34")  # TP1: 1 of 3
    h.publish(build_open(h.now(), "t-ptp", take_profits=MTP))
    h.cycle()
    assert tp_legs(h, "t-ptp", A) == [(D(3), D(0), D(3)), (D(4), D(0), D(4))]
    h.market("65100")  # TP1 triggers; only 1 contract fills; price still beyond TP1
    h.cycle()
    a = h.ex("t-ptp", A)
    assert tp_legs(h, "t-ptp", A) == [(D(3), D(1), D(2)), (D(4), D(0), D(4))]
    assert h.pos(A)["qty"] == 6
    stops = h.venue_orders(A, "STOP_MARKET", "OPEN")
    assert len(stops) == 1 and D(stops[0]["qty"]) == 6, "stop covers actual remaining exposure"
    assert (a["stop_coverage"], a["tp_coverage"]) == ("VERIFIED", "DEGRADED"), "re-arm refused at market"
    # degraded account is gated for NEW exposure (healthy beta unaffected by alpha's state)
    h.publish(build_open(h.now(), "t-ptp-next"))
    h.cycle()
    assert h.ex("t-ptp-next", A)["reason_code"] == "ACCOUNT_DEGRADED"
    assert h.ex("t-ptp-next", B)["reason_code"] == "OVERLAPPING_TRADE"
    h.market("64900")
    h.clock.advance(11)  # past the bounded backoff
    h.cycle()
    tps = h.venue_orders(A, "TAKE_PROFIT_MARKET", "OPEN")
    assert sorted(D(o["qty"]) for o in tps) == [D(2), D(4)], "only the REMAINING 2 re-armed (not 3 / 3.5)"
    assert h.ex("t-ptp", A)["tp_coverage"] == "COMPLETE"
    h.market("66100")
    h.cycle()
    e = h.ex("t-ptp", A)
    assert e["state"] == "CLOSED" and h.pos(A)["qty"] == 0 and h.pos(A)["side"] is None
    assert tp_legs(h, "t-ptp", A) == [(D(3), D(3), D(0)), (D(4), D(4), D(0))]
    exits = sum((f["qty"] for f in h.j.fills_for(e["id"], ("STOP", "TP", "CLOSE"))), D(0))
    assert exits == e["filled_qty"] == 7


def test_persistent_tp_failure_bounded_backoff_and_durable_escalation(mk):
    h = mk()
    h.sim.add_fault("sim_beta", "place_tp", "REJECT", account=B, param="SIM_TP_DOWN", count=100)
    h.publish(build_open(h.now(), "t-esc"))
    h.cycle()
    attempts = lambda: h.j.q1("SELECT COUNT(*) AS n FROM orders WHERE leg='TP' AND account_id=?", (B,))["n"]
    n0 = attempts()
    for _ in range(5):
        h.cycle()  # inside backoff: no new attempts
    assert attempts() == n0
    for _ in range(8):
        h.clock.advance(400)  # > max backoff
        h.cycle()
    b = h.ex("t-esc", B)
    assert b["state"] == "OPEN_PROTECTED" and b["stop_coverage"] == "VERIFIED" and h.pos(B)["qty"] == 75
    assert b["tp_coverage"] == "DEGRADED" and b["tp_fail_count"] >= 5
    assert attempts() == n0 + 8, "exactly one bounded attempt per expired backoff"
    inc = [i for i in h.j.open_incidents(B) if i["kind"] == "TP_PLACEMENT_PERSISTENT"]
    assert len(inc) == 1, "durable escalation, raised once"
    assert h.j.q1("SELECT COUNT(*) AS n FROM alerts WHERE code='TP_DEGRADED'")["n"] == 1
    assert not h.j.account(B)["entries_blocked"], "dynamic gate, not a manual block"
    assert h.ex("t-esc", A)["tp_coverage"] == "COMPLETE", "healthy account unaffected"


def test_stop_resize_failure_keeps_the_covering_stop(mk):
    h = mk()
    h.publish(build_open(h.now(), "t-rsz", take_profits=MTP))
    h.cycle()
    h.sim.add_fault("sim_beta", "place_stop", "REJECT", account=B, param="SIM_STOP_DOWN")
    h.market("65100")  # beta TP1 fills (35/75); beta cannot amend -> place-new-then-cancel-old
    h.cycle()
    b = h.ex("t-rsz", B)
    assert h.pos(B)["qty"] == 40
    stops = h.venue_orders(B, "STOP_MARKET", "OPEN")
    assert len(stops) == 1 and D(stops[0]["qty"]) == 75, "old (oversize, reduce-only) stop retained"
    assert b["state"] == "OPEN_PROTECTED" and b["stop_coverage"] == "VERIFIED"
    assert not h.j.open_incidents(B) and not h.venue_orders(B, "CLOSE_MARKET"), "no emergency close"
    h.clock.advance(11)
    h.cycle()
    stops = h.venue_orders(B, "STOP_MARKET", "OPEN")
    assert len(stops) == 1 and D(stops[0]["qty"]) == 40, "resized after backoff"


def test_delayed_observation_after_timeout_is_not_treated_as_absent(mk):
    h = mk()
    h.sim.add_fault("sim_alpha", "place_stop", "TIMEOUT_AFTER_ACCEPT", account=A)
    h.sim.add_fault("sim_alpha", "get_order", "STALE_NOT_FOUND", account=A, count=2)
    h.publish(build_open(h.now(), "t-dly"))
    h.cycle()
    a = h.ex("t-dly", A)
    assert a["stop_coverage"] == "UNVERIFIED" and not h.j.open_incidents(A)
    assert len(h.venue_orders(A, "STOP_MARKET")) == 1
    h.clock.advance(2)
    h.cycle()
    a = h.ex("t-dly", A)
    assert a["stop_coverage"] == "VERIFIED" and a["state"] == "OPEN_PROTECTED"
    assert len(h.venue_orders(A, "STOP_MARKET")) == 1, "never resubmitted under a new identity"
    assert a["max_unprotected_seconds"] >= 2, "unprotected interval measured, not assumed zero"


def test_stale_exit_after_flatness_cannot_reverse(mk):
    h = mk()
    h.publish(build_open(h.now(), "t-stale"))
    h.cycle()
    h.sim.add_fault("sim_alpha", "cancel", "REJECT", account=A, count=2)
    h.market("63900", accounts=(A,))  # stop fills on the venue
    h.cycle()  # orphan TP cancel fails twice
    assert h.venue_orders(A, "TAKE_PROFIT_MARKET", "OPEN"), "stale TP still working"
    h.market("66100", accounts=(A,))  # stale TP triggers with no position
    p = h.pos(A)
    assert p["qty"] == 0 and p["side"] is None, "reduce-only stale exit did not open/reverse"
    h.cycle()
    a = h.ex("t-stale", A)
    assert a["state"] == "CLOSED" and a["close_reason"] == "STOP_LOSS"
    h.assert_flat_and_clean(A)


def test_long_fill_above_estimate_stays_within_plan(mk):
    h = mk()
    h.sim.add_fault("sim_alpha", "place_entry", "SHIFT_BOOK", account=A, param="50")
    h.publish(build_open(h.now(), "t-lsz"))
    h.cycle()
    a = h.ex("t-lsz", A)
    assert a["avg_fill"] == D("64550.1") > D(a["plan"]["expected_fill"])
    assert a["actual_margin"] <= a["planned_margin"], "LONG sized at the IOC ceiling: upper bound"
    assert not [x for x in h.alerts if x["code"] == "ACTUAL_MARGIN_ABOVE_PLAN"]


def test_short_favourable_fill_above_plan_is_reported_not_concealed(mk):
    h = mk()
    h.sim.add_fault("sim_alpha", "place_entry", "SHIFT_BOOK", account=A, param="3000")
    h.publish(build_open(h.now(), "t-ssz", direction="SHORT", stop="68500.10",
                         take_profits=[("63000.10", "1")]))
    h.cycle()
    a = h.ex("t-ssz", A)
    assert a["actual_margin"] > a["planned_margin"], "a sell floor is not an upper notional bound"
    assert a["actual_margin"] < D("110"), "below the configured hard limit -> position retained"
    assert a["leverage"] == 5 and D(a["exit_plan"]["stop"]) == D("68500.10"), "nothing adjusted"
    assert a["state"] == "OPEN_PROTECTED"
    assert [x for x in h.alerts if x["code"] == "ACTUAL_MARGIN_ABOVE_PLAN"]


def test_failed_emergency_close_without_valid_stop_raises_incident_until_resolved(mk):
    h = mk(lambda d: d["accounts"][0].__setitem__("max_initial_margin_hard", "92.00"))
    h.sim.add_fault("sim_alpha", "place_entry", "SHIFT_BOOK", account=A, param="3000")
    h.sim.add_fault("sim_alpha", "place_close", "REJECT", account=A, count=4)
    h.sim.add_fault("sim_alpha", "place_stop", "REJECT", account=A, count=4)
    h.publish(build_open(h.now(), "t-unp", direction="SHORT"))
    h.cycle()
    a = h.ex("t-unp", A)
    assert a["state"] == "CLOSING" and a["stop_coverage"] in ("NONE", "UNVERIFIED")
    assert any(i["kind"] == "UNPROTECTED_EXPOSURE" for i in h.j.open_incidents(A))
    for _ in range(4):
        h.clock.advance(3)
        h.cycle()
    a = h.ex("t-unp", A)
    assert a["state"] == "CLOSED" and h.pos(A)["qty"] == 0
    assert a["max_unprotected_seconds"] > 0, "unprotected interval measured"
    assert not [i for i in h.j.open_incidents(A) if i["execution_id"] == a["id"]]


def test_journal_busy_blocks_ingestion_without_external_effect(mk):
    h = mk(busy_timeout_ms=200)
    other = sqlite3.connect(str(h.cfg.journal_path), isolation_level=None, timeout=1)
    other.execute("BEGIN IMMEDIATE")  # another writer holds the lock (test storage only)
    try:
        p = h.publish(build_open(h.now(), "t-busy"))
        res = h.engine.run_cycle()
        assert "ingest" in res["errors"], res
        assert p.exists() and not h.venue_orders(A) and not h.venue_orders(B)
    finally:
        other.execute("ROLLBACK")
        other.close()
    h.cycle()
    assert h.ex("t-busy", A)["state"] == "OPEN_PROTECTED" and len(h.venue_orders(A, "ENTRY_LIMIT_IOC")) == 1


def test_commit_failure_is_rolled_back_and_connection_stays_usable(mk):
    h = mk()
    h.publish(build_open(h.now(), "t-cf"))
    h.journal.inject_failure("commit", 1)  # the ingest transaction fails at COMMIT
    res = h.engine.run_cycle()
    assert "ingest" in res["errors"] and h.j.event_by_id("t-cf-r1-open") is None
    assert not h.journal.conn.in_transaction, "failed COMMIT rolled back (not wedged)"
    h.cycle()
    assert h.ex("t-cf", A)["state"] == "OPEN_PROTECTED"


def test_intent_persistence_failure_never_submits_blind(mk, monkeypatch):
    h = mk()
    real = h.journal.insert
    state = {"armed": True}

    def flaky(table, **f):
        if table == "orders" and state["armed"] and f.get("account_id") == A:
            state["armed"] = False
            raise sqlite3.OperationalError("disk I/O error (test fault: order intent)")
        return real(table, **f)
    monkeypatch.setattr(h.journal, "insert", flaky)
    h.publish(build_open(h.now(), "t-ipf"))
    h.cycle()
    assert not h.venue_orders(A), "no entry without a persisted intent"
    h.cycle()
    a, b = h.ex("t-ipf", A), h.ex("t-ipf", B)
    assert a["state"] == "ENTRY_CANCELLED" and a["reason_code"] == "RECOVERY_NOT_SUBMITTED"
    assert not h.venue_orders(A) and b["state"] == "OPEN_PROTECTED", "other account routed on resume"


def test_persistence_failure_after_venue_ack_is_recovered_by_reconciliation(mk, monkeypatch):
    h = mk()
    ad = h.engine.adapters[A]
    real = ad.place_order

    def ack_then_fail(req):
        rep = real(req)
        if req.kind == "ENTRY_LIMIT_IOC":
            h.journal.inject_failure("begin", 1)  # next local write after the ack fails
        return rep
    monkeypatch.setattr(ad, "place_order", ack_then_fail)
    h.publish(build_open(h.now(), "t-ack"))
    h.cycle()
    assert h.pos(A)["qty"] == 7, "exposure exists on the venue"
    h.cycle()
    a = h.ex("t-ack", A)
    assert a["state"] == "OPEN_PROTECTED" and a["filled_qty"] == 7
    assert len(h.venue_orders(A, "ENTRY_LIMIT_IOC")) == 1, "reconciled by client id, not resubmitted"


def test_archive_failure_does_not_block_processing(mk, monkeypatch):
    h = mk()
    real = h.engine.inbox.archive
    calls = {"n": 0}

    def fail_once(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("access denied (test fault)")
        return real(path)
    monkeypatch.setattr(h.engine.inbox, "archive", fail_once)
    p = h.publish(build_open(h.now(), "t-arc"))
    h.cycle()
    assert p.exists() and h.ex("t-arc", A)["state"] == "OPEN_PROTECTED"
    assert [x for x in h.alerts if x["code"] == "INBOX_MOVE_FAILED"]
    h.cycle()
    assert not p.exists()
    assert h.j.q1("SELECT COUNT(*) AS n FROM executions WHERE trade_id='t-arc'")["n"] == 2


def test_failing_account_cannot_starve_a_healthy_account(mk):
    h = mk()
    h.sim.add_fault("sim_beta", "place_entry", "TIMEOUT_AFTER_ACCEPT", account=B)
    h.sim.add_fault("sim_beta", "get_order", "TIMEOUT_BEFORE_ACCEPT", account=B, count=1000)
    h.publish(build_open(h.now(), "t-starve"))
    h.cycle()
    h.market("66100")
    h.cycle()
    assert h.ex("t-starve", A)["state"] == "CLOSED" and h.ex("t-starve", A)["close_reason"] == "TAKE_PROFIT"
    assert h.ex("t-starve", B)["state"] == "ENTRY_UNKNOWN"
    assert h.engine._acct_errors.get(B, 0) <= h.engine.MAX_ACCOUNT_ERRORS_PER_CYCLE


def test_duplicate_json_keys_rejected_in_unsigned_events_too():
    with pytest.raises(Rejection) as ei:
        load_event_json('{"event_id": "a", "event_id": "b"}')
    assert str(ei.value.code) == "DUPLICATE_JSON_KEY"


def test_identical_alerts_are_deduplicated(mk):
    h = mk()
    for _ in range(5):
        h.engine.alert("WARNING", "X_TEST", "same message", A)
    assert h.j.q1("SELECT COUNT(*) AS n FROM alerts WHERE code='X_TEST'")["n"] == 1
    h.clock.advance(301)
    h.engine.alert("WARNING", "X_TEST", "same message", A)
    row = h.j.q("SELECT detail FROM alerts WHERE code='X_TEST' ORDER BY id")[-1]
    assert row["detail"].get("suppressed_repeats") == 4


# ---------------------------------------------------------------- ECC review (round 2) regressions
def test_not_found_counter_resets_when_order_is_seen_again(mk):
    h = mk()
    h.publish(build_open(h.now(), "t-nf"))
    h.cycle()
    for _ in range(4):  # four separate single misses over time (each followed by a sighting)
        h.sim.add_fault("sim_alpha", "get_order", "STALE_NOT_FOUND", account=A)
        h.cycle()
        h.clock.advance(60)
        h.cycle()
    stops = h.venue_orders(A, "STOP_MARKET")
    assert len(stops) == 1 and stops[0]["status"] == "OPEN", "sporadic misses never demote a live stop"
    assert h.j.q1("SELECT not_found_checks FROM orders WHERE leg='STOP' AND account_id=?", (A,))["not_found_checks"] == 0


def test_failed_cancel_of_old_stop_after_resize_does_not_churn(mk):
    h = mk()
    h.publish(build_open(h.now(), "t-cc", take_profits=MTP))
    h.cycle()
    h.sim.add_fault("sim_beta", "cancel", "REJECT", account=B, count=3)
    h.market("65100")  # beta TP1 fills; place-new-then-cancel-old; old cancel fails
    h.cycle()
    n_after_first = len(h.venue_orders(B, "STOP_MARKET"))
    for _ in range(5):
        h.clock.advance(20)
        h.cycle()
    assert len(h.venue_orders(B, "STOP_MARKET")) == n_after_first, "no repeated stop placement"
    opens = h.venue_orders(B, "STOP_MARKET", "OPEN")
    assert len(opens) == 1 and D(opens[0]["qty"]) == 40


def test_unresolved_stop_while_closing_is_bounded_and_reported(mk):
    h = mk(lambda d: d["accounts"][0].__setitem__("max_initial_margin_hard", "92.00"))
    h.sim.add_fault("sim_alpha", "place_entry", "SHIFT_BOOK", account=A, param="3000")
    h.sim.add_fault("sim_alpha", "place_close", "REJECT", account=A, count=6)
    h.sim.add_fault("sim_alpha", "place_stop", "TIMEOUT_BEFORE_ACCEPT", account=A, count=1)
    h.publish(build_open(h.now(), "t-cu", direction="SHORT"))
    h.cycle()
    assert h.ex("t-cu", A)["stop_coverage"] == "UNVERIFIED"
    for _ in range(3):
        h.clock.advance(2)
        h.cycle()
    a = h.ex("t-cu", A)
    assert a["state"] == "CLOSING" and a["stop_coverage"] == "VERIFIED", "replaced after bounded checks"
    assert len(h.venue_orders(A, "STOP_MARKET", "OPEN")) == 1
    for _ in range(4):
        h.clock.advance(2)
        h.cycle()
    assert h.ex("t-cu", A)["state"] == "CLOSED" and h.pos(A)["qty"] == 0


def test_v1_journal_is_migrated_additively_with_data_preserved(tmp_path):
    import pathlib
    from aseke_exec.journal import SCHEMA_VERSION, Journal
    db = tmp_path / "journal.db"
    schema = (pathlib.Path(__file__).parent / "fixtures/journal_v1_schema.sql").read_text(encoding="utf-8")
    c = sqlite3.connect(db)
    c.executescript(schema)
    c.execute("INSERT INTO meta VALUES ('schema_version','1')")
    c.execute("INSERT INTO events (event_id, content_hash, trade_id, event_type, revision, producer, "
              "environment, created_at, received_at, canonical_json, process_status) VALUES "
              "('e1','h','t1','OPEN',1,'p','SIMULATION','x','x','{}','APPLIED')")
    c.commit()
    c.close()
    ro = Journal(db, readonly=True)
    assert ro.schema_version() == 1
    ro.close()
    j = Journal(db)
    cols = {r[1] for r in j.conn.execute("PRAGMA table_info(executions)")}
    assert {"stop_coverage", "tp_coverage", "unprotected_since", "tp_fail_count"} <= cols
    assert j.schema_version() == SCHEMA_VERSION and j.event_by_id("e1")["process_status"] == "APPLIED"
    j.close()
