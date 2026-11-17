"""Strict authenticated-envelope ingestion (HMAC-SHA-256): happy path, rejection, replay,
poisoning, key scope, revocation/rotation, fail-closed missing keys, action-specific
timestamps. All keys here are TEST-ONLY constants, never real credentials."""

import hashlib
import json
import logging
import os
from datetime import timedelta
from pathlib import Path

import pytest

from aseke_exec.auth import (
    AuthConfig, Authenticator, AuthRejection, KeySpec, build_envelope, compute_signature,
    signing_bytes, strict_loads,
)
from aseke_exec.clock import ManualClock, parse_utc
from aseke_exec.examples import build_close, build_open
from aseke_exec.inbox import publish_event
from aseke_exec.scenarios import A, B, Harness

ROOT = Path(__file__).resolve().parents[1]
TEST_ONLY_KEY = b"TEST-ONLY-HMAC-KEY--not-a-secret--aseke-exec-0001"  # clearly synthetic
TEST_ONLY_KEY2 = b"TEST-ONLY-HMAC-KEY--not-a-secret--aseke-exec-0002"
OTHER_KEY = b"TEST-ONLY-HMAC-KEY--WRONG--------aseke-exec-9999"
AUD = "aseke-exec:test"
PRODUCER = "aseke-dummy-publisher"


def _key(kid, env, strategies=("lab_btc_breakout",), status="active"):
    return {"key_id": kid, "producer": PRODUCER, "strategies": list(strategies),
            "environments": ["SIMULATION"], "audiences": [AUD], "status": status, "secret_env": env}


def strict_profile(d, keys=None):
    d["auth"] = {"mode": "strict", "audience": AUD,
                 "keys": keys or [_key("t-k1", "ASEKE_TEST_HMAC_K1"),
                                  _key("t-k2", "ASEKE_TEST_HMAC_K2", strategies=("other_strategy",))]}


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.setenv("ASEKE_TEST_HMAC_K1", TEST_ONLY_KEY.hex())
    monkeypatch.setenv("ASEKE_TEST_HMAC_K2", TEST_ONLY_KEY2.hex())
    hh = Harness(tmp_path, strict_profile)
    hh.start()
    yield hh
    if hh.journal is not None:
        hh.journal.close()
    hh.sim.close()


def signed(h, ev, key=TEST_ONLY_KEY, kid="t-k1", audience=AUD, issued=None, nonce=None):
    return build_envelope(ev, key=key, key_id=kid, audience=audience, issued_at=issued or h.now(),
                          nonce=nonce)


def pub(h, doc, name):
    return publish_event(h.cfg.inbox_dir, doc, filename=name)


def ingest_outcome(h, name):
    r = h.j.q1("SELECT outcome, code FROM ingest_log WHERE file_name=? ORDER BY id DESC LIMIT 1", (name,))
    return (r["outcome"], r["code"]) if r else None


def test_signed_open_happy_path_and_no_key_material_persisted(h):
    pub(h, signed(h, build_open(h.now(), "t-s1")), "a.json")
    h.cycle()
    for acct in (A, B):
        assert h.ex("t-s1", acct)["state"] == "OPEN_PROTECTED"
    ev = h.event("t-s1-r1-open")
    assert ev["auth_mode"] == "HMAC" and ev["auth_key_id"] == "t-k1" and ev["envelope_hash"]
    h.journal.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = h.cfg.journal_path.read_bytes()
    assert TEST_ONLY_KEY not in blob and TEST_ONLY_KEY.hex().encode() not in blob
    assert all(TEST_ONLY_KEY.hex() not in json.dumps(a, default=str) for a in h.alerts)


def test_strict_mode_refuses_unsigned_without_fallback(h):
    pub(h, build_open(h.now(), "t-u1"), "u.json")
    h.cycle()
    assert ingest_outcome(h, "u.json") == ("AUTH_REJECTED", "AUTH_REQUIRED")
    assert h.j.event_by_id("t-u1-r1-open") is None and not h.has_ex("t-u1", A)
    assert (h.cfg.inbox_dir.parent / "quarantine" / "u.json").exists()


@pytest.mark.parametrize("case,code", [
    ("tampered_payload", "AUTH_BAD_SIGNATURE"),
    ("wrong_key", "AUTH_BAD_SIGNATURE"),
    ("unknown_key_id", "AUTH_UNKNOWN_KEY"),
    ("strategy_out_of_scope", "AUTH_SCOPE_DENIED"),
    ("other_audience", "AUTH_SCOPE_DENIED"),
    ("live_environment", "AUTH_SCOPE_DENIED"),
    ("inner_producer_mismatch", "AUTH_ENVELOPE_MISMATCH"),
    ("bad_signature_format", "ENVELOPE_INVALID"),
    ("future_issued_at", "AUTH_TIMESTAMP_INVALID"),
])
def test_rejections_happen_before_any_trade_state(h, case, code):
    ev = build_open(h.now(), "t-r1")
    if case == "tampered_payload":
        env = signed(h, ev)
        env["payload"]["open"]["leverage"] = 50
    elif case == "wrong_key":
        env = signed(h, ev, key=OTHER_KEY)
    elif case == "unknown_key_id":
        env = signed(h, ev, kid="t-k9")
    elif case == "strategy_out_of_scope":
        env = signed(h, ev, key=TEST_ONLY_KEY2, kid="t-k2")
    elif case == "other_audience":
        env = signed(h, ev, audience="aseke-exec:someone-else")
    elif case == "live_environment":
        env = signed(h, build_open(h.now(), "t-r1", environment="LIVE"))
    elif case == "inner_producer_mismatch":
        env = signed(h, ev)
        env["payload"]["producer"]["name"] = "aseke-btc-lab"
        env["signature"] = compute_signature(TEST_ONLY_KEY, env)  # validly re-signed
    elif case == "bad_signature_format":
        env = signed(h, ev)
        env["signature"] = env["signature"].upper()
    else:
        env = signed(h, ev, issued=h.now() + timedelta(minutes=5))
    pub(h, env, "r.json")
    h.cycle()
    assert ingest_outcome(h, "r.json") == ("AUTH_REJECTED", code)
    assert h.j.trade("t-r1") is None and h.j.event_by_id("t-r1-r1-open") is None
    assert not h.venue_orders(A) and not h.venue_orders(B)


def test_duplicate_keys_and_floats_in_envelope_rejected(h):
    good = json.dumps(signed(h, build_open(h.now(), "t-d1")))
    dup = good[:-1] + ', "nonce": "00112233445566778899aabbccddeeff"}'
    (h.cfg.inbox_dir / "dup.json").write_text(dup, encoding="utf-8")
    (h.cfg.inbox_dir / "flt.json").write_text(good.replace('"leverage": 5', '"leverage": 5.0'),
                                              encoding="utf-8")
    h.cycle()
    assert ingest_outcome(h, "dup.json") == ("INVALID", "DUPLICATE_JSON_KEY")
    assert ingest_outcome(h, "flt.json") == ("INVALID", "INVALID_NUMBER")
    assert h.j.trade("t-d1") is None


def test_resigned_duplicate_is_one_event_and_changed_payload_conflicts(h):
    ev = build_open(h.now(), "t-dup")
    pub(h, signed(h, ev, nonce="aa" * 16), "1.json")
    pub(h, signed(h, ev, nonce="bb" * 16), "2.json")  # same event, new envelope
    changed = json.loads(json.dumps(ev))
    changed["open"]["leverage"] = 6
    pub(h, signed(h, changed, nonce="cc" * 16), "3.json")
    h.cycle()
    assert ingest_outcome(h, "2.json")[0] == "DUPLICATE"
    assert ingest_outcome(h, "3.json")[0] == "CONFLICT"
    assert h.j.q1("SELECT COUNT(*) AS n FROM events")["n"] == 1
    assert len(h.venue_orders(A, "ENTRY_LIMIT_IOC")) == 1


def test_invalid_signed_close_does_not_poison_future_open(h):
    bad_close = signed(h, build_close(h.now(), "t-p", 2), key=OTHER_KEY)
    forged_open = signed(h, build_open(h.now(), "t-p", leverage=50), key=OTHER_KEY)
    pub(h, bad_close, "c.json")
    pub(h, forged_open, "f.json")
    h.cycle()
    assert h.j.trade("t-p") is None, "no tombstone / no event-id consumption before authentication"
    pub(h, signed(h, build_open(h.now(), "t-p")), "o.json")
    h.cycle()
    assert h.ex("t-p", A)["state"] == "OPEN_PROTECTED" and h.ex("t-p", A)["leverage"] == 5


def test_authenticated_close_before_open_tombstones(h):
    pub(h, signed(h, build_close(h.now(), "t-tb", 2)), "c.json")
    h.cycle()
    pub(h, signed(h, build_open(h.now(), "t-tb")), "o.json")
    h.cycle()
    assert h.event("t-tb-r1-open")["process_code"] == "TRADE_TOMBSTONED"
    assert not h.has_ex("t-tb", A)


def test_action_specific_timestamps_and_entry_expiry_still_applies(h):
    pub(h, signed(h, build_open(h.now(), "t-ts")), "o.json")
    h.cycle()
    h.clock.advance(3600)
    h.market("64500")
    old_open = signed(h, build_open(h.now() - timedelta(minutes=10), "t-ts2"),
                      issued=h.now() - timedelta(minutes=10))
    late_close = signed(h, build_close(h.now() - timedelta(minutes=55), "t-ts", 2),
                        issued=h.now() - timedelta(minutes=55))
    pub(h, old_open, "old.json")
    pub(h, late_close, "late.json")
    h.cycle()
    assert ingest_outcome(h, "old.json") == ("AUTH_REJECTED", "AUTH_TIMESTAMP_INVALID")
    assert h.ex("t-ts", A)["state"] == "CLOSED", "a delayed authorized CLOSE is still applied"
    # a freshly signed envelope does not bypass the payload's own entry expiry
    stale = build_open(h.now() - timedelta(seconds=30), "t-ts3", expires_in_s=10)
    pub(h, signed(h, stale), "s.json")
    h.cycle()
    assert h.ex("t-ts3", A)["reason_code"] == "SIGNAL_EXPIRED"


def test_missing_key_fails_closed_but_existing_positions_stay_managed(h, monkeypatch):
    pub(h, signed(h, build_open(h.now(), "t-mk")), "o.json")
    h.cycle()
    monkeypatch.delenv("ASEKE_TEST_HMAC_K1")
    pending = pub(h, signed(h, build_open(h.now(), "t-mk2"), key=TEST_ONLY_KEY), "held.json")
    h.market("66100")  # existing TP must still be managed
    h.cycle()
    assert pending.exists(), "file is HELD in the inbox, not consumed"
    # durable HELD record (not consumed, no event/trade state); see security-review finding 8
    assert ingest_outcome(h, "held.json") == ("HELD", "AUTH_KEY_UNAVAILABLE") and h.j.trade("t-mk2") is None
    assert h.j.event_by_id("t-mk2-r1-open") is None
    assert h.ex("t-mk", A)["state"] == "CLOSED" and h.ex("t-mk", A)["close_reason"] == "TAKE_PROFIT"
    monkeypatch.setenv("ASEKE_TEST_HMAC_K1", TEST_ONLY_KEY.hex())
    h.market("64500")
    h.cycle()
    assert not pending.exists() and h.ex("t-mk2", A)["state"] == "OPEN_PROTECTED"


def test_revocation_rejects_pending_open_but_applies_pending_close_and_rotation(h, monkeypatch):
    monkeypatch.setenv("ASEKE_TEST_HMAC_K3", TEST_ONLY_KEY2.hex())
    pub(h, signed(h, build_open(h.now(), "t-rv")), "o1.json")
    h.cycle()
    pub(h, signed(h, build_close(h.now(), "t-rv", 2)), "c1.json")
    pub(h, signed(h, build_open(h.now(), "t-rv2")), "o2.json")
    h.engine.ingest()  # journaled (authenticated at ingestion) but not yet processed
    h.reconfigure(lambda d: strict_profile(d, [_key("t-k1", "ASEKE_TEST_HMAC_K1", status="revoked"),
                                               _key("t-k3", "ASEKE_TEST_HMAC_K3")]))
    h.cycle()
    assert h.event("t-rv2-r1-open")["process_code"] == "AUTH_KEY_REVOKED"
    assert not h.has_ex("t-rv2", A)
    assert h.ex("t-rv", A)["state"] == "CLOSED", "exposure-reducing CLOSE still applied"
    pub(h, signed(h, build_open(h.now(), "t-rv3")), "o3.json")  # old key: refused
    pub(h, signed(h, build_open(h.now(), "t-rv4"), key=TEST_ONLY_KEY2, kid="t-k3"), "o4.json")
    h.cycle()
    assert ingest_outcome(h, "o3.json") == ("AUTH_REJECTED", "AUTH_KEY_REVOKED")
    assert h.ex("t-rv4", A)["state"] == "OPEN_PROTECTED", "rotated key accepted"


def test_deterministic_fixture_and_documented_signing_bytes():
    d = ROOT / "examples/signals/envelopes"
    text = (d / "open_long_reference.signed.json").read_text(encoding="utf-8")
    env = strict_loads(text)
    meta = json.loads((d / "FIXTURE.json").read_text(encoding="utf-8"))
    assert meta["key_is_test_only"] is True
    key = bytes.fromhex(meta["test_only_key_hex"])
    assert compute_signature(key, env) == env["signature"] == meta["signature"]
    assert signing_bytes(env).startswith(b'aseke.exec.envelope/1\n{"alg":"HMAC-SHA256"')
    assert hashlib.sha256(signing_bytes(env)).hexdigest() == meta["signing_bytes_sha256"]
    clock = ManualClock(parse_utc(env["issued_at"]))
    cfg = AuthConfig("strict", env["audience"], {"fixture-k1": KeySpec(
        "fixture-k1", env["producer"], frozenset({"lab_btc_breakout"}), frozenset({"SIMULATION"}),
        frozenset({env["audience"]}), "active", secret_env="ASEKE_FIXTURE_KEY_TEST_ONLY")})
    os.environ["ASEKE_FIXTURE_KEY_TEST_ONLY"] = key.hex()
    try:
        res = Authenticator(cfg, clock).unwrap(text)
    finally:
        del os.environ["ASEKE_FIXTURE_KEY_TEST_ONLY"]
    assert res.mode == "HMAC" and res.payload["event_id"] == "fixture-long-1-r1-open"
    with pytest.raises(AuthRejection) as ei:
        Authenticator(cfg, clock).unwrap(text)  # key unset -> fail closed (HOLD)
    assert ei.value.hold


# ---------------------------------------------------------------- ECC security-review regressions
def test_poison_files_are_quarantined_and_never_stall_the_inbox(h):
    inbox = h.cfg.inbox_dir
    (inbox / "0000_badutf8.json").write_bytes(b'{"x": "\xff\xfe"}')
    (inbox / "0001_deep.json").write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
    env = signed(h, build_open(h.now(), "t-poison"))
    raw = json.dumps(env).replace('"t-poison"', '"t-poison' + chr(92) + 'ud800"', 1)  # JSON escape
    (inbox / "0002_surrogate.json").write_text(raw, encoding="utf-8")
    pub(h, signed(h, build_close(h.now(), "t-after", 2)), "0003_legit.json")  # sorts AFTER the poison
    res = h.engine.run_cycle()
    assert "ingest" not in res["errors"], res["errors"]
    for n in ("0000_badutf8.json", "0001_deep.json", "0002_surrogate.json"):
        assert (h.cfg.inbox_dir.parent / "quarantine" / n).exists(), n
    assert h.j.trade("t-after")["status"] == "TOMBSTONED", "legitimate file behind the poison processed"


def test_revoked_key_close_cannot_tombstone_and_scope_is_rechecked_at_apply(h):
    pub(h, signed(h, build_close(h.now(), "t-rk", 2)), "c.json")
    pub(h, signed(h, build_open(h.now(), "t-sc")), "o.json")
    h.engine.ingest()  # authenticated + journaled, not yet applied
    # narrow k1's scope (strategy removed) and revoke nothing else
    h.reconfigure(lambda d: strict_profile(d, [_key("t-k1", "ASEKE_TEST_HMAC_K1", strategies=("other",)),
                                               _key("t-k2", "ASEKE_TEST_HMAC_K2")]))
    h.cycle()
    assert h.j.trade("t-rk") is None, "no tombstone from a key that no longer authorizes the strategy"
    assert h.event("t-rk-r2-close")["process_code"] == "AUTH_KEY_REVOKED"
    assert h.event("t-sc-r1-open")["process_code"] == "AUTH_KEY_REVOKED" and not h.has_ex("t-sc", A)


def test_hot_reload_cannot_switch_auth_profile(h):
    from aseke_exec.config import ConfigError
    with pytest.raises(ConfigError):
        h.reconfigure(lambda d: d.__setitem__("auth", {"mode": "simulation-compat-unsigned"}))
    assert h.engine.cfg.auth.mode == "strict"
    pub(h, build_open(h.now(), "t-hr"), "u.json")
    h.cycle()
    assert ingest_outcome(h, "u.json") == ("AUTH_REJECTED", "AUTH_REQUIRED")


def test_key_file_with_bom_loads_and_held_files_are_logged_once(tmp_path, h, monkeypatch):
    from aseke_exec.auth import load_secret
    kf = tmp_path / "k.key"
    kf.write_text("﻿" + TEST_ONLY_KEY.hex() + "\r\n", encoding="utf-8")
    spec = KeySpec("x", PRODUCER, frozenset({"s"}), frozenset({"SIMULATION"}), frozenset({AUD}),
                   "active", secret_file=kf)
    assert load_secret(spec) == TEST_ONLY_KEY
    monkeypatch.delenv("ASEKE_TEST_HMAC_K1")
    pub(h, signed(h, build_open(h.now(), "t-held")), "held.json")
    for _ in range(3):
        h.cycle()
    assert h.j.q1("SELECT COUNT(*) AS n FROM ingest_log WHERE outcome='HELD'")["n"] == 1
