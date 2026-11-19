"""Phase 2C: corrected synthetic soak workload, funded/low-capital TEST-ONLY profiles, and the
diagnostic soak report. The strict contract is NOT loosened: invalid external signals are still
quarantined; only the harness generator changed."""

import json
import logging
import random
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from aseke_exec import soak as soak_mod
from aseke_exec.auth import build_envelope
from aseke_exec.cli import main
from aseke_exec.clock import SystemClock
from aseke_exec.contract import load_event_json, parse_event
from aseke_exec.examples import build_open
from aseke_exec.inbox import publish_event, signal_filename
from aseke_exec.num import floor_to_step, is_multiple
from aseke_exec.reasons import Rejection
from aseke_exec.soak import FUNDING_PROFILES, _journal_summary, run_soak
from aseke_exec.soakgen import (BASE_OFFSETS, MarketWalk, SoakWorkloadError, build_soak_open,
                                common_tick, exit_levels, legacy_exit_levels, replay,
                                validate_generated)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
VENUE_TICKS = [Decimal("0.1"), Decimal("0.05")]  # sim_alpha / sim_beta in config/*.toml
HIST = dict(seed=7, cycles=32702, trade_every_cycles=8, initial_price=Decimal("64500.0"))


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.WARNING)
    yield
    logging.disable(logging.NOTSET)


def _cfg(tmp_path, name="sim.toml", **replace):
    p = tmp_path / f"cfg-{name}"
    text = (ROOT / "config" / name).read_text(encoding="utf-8")
    for a, b in replace.items():
        assert a in text, a
        text = text.replace(a, b)
    p.write_text(text, encoding="utf-8")
    return p


# ------------------------------------------------------------------ generator units
@pytest.mark.parametrize("tick", [Decimal("0.1"), Decimal("0.05"), Decimal("0.5")])
@pytest.mark.parametrize("ref", ["1000", "1000.5", "1455", "1499.5", "1500", "1500.5", "2000",
                                 "2999.5", "3000", "3000.5", "64500", "98205"])
@pytest.mark.parametrize("long_", [True, False])
def test_levels_are_positive_ordered_tick_aligned_and_parse(tick, ref, long_):
    ref = floor_to_step(Decimal(ref), tick)
    lv = exit_levels(ref, long_, tick)
    tp1, tp2 = lv.take_profits
    if long_:
        assert 0 < lv.stop < ref < tp1 < tp2
    else:
        assert 0 < tp2 < tp1 < ref < lv.stop
    for p in (lv.stop, tp1, tp2):
        assert is_multiple(p, tick) and p >= ref / 2, (p, ref)
    ev = build_soak_open(NOW, "soak-000000", ref, long_, lv, max_holding_seconds=30)
    validate_generated(ev, [tick])  # production parser + tick check
    op = parse_event(load_event_json(json.dumps(ev))).open
    assert [t.quantity_fraction for t in op.exits.take_profits] == [Decimal("0.5")] * 2


def test_base_offsets_preserved_where_they_remain_valid():
    for ref in (Decimal("3000"), Decimal("64500.1"), Decimal("98205")):
        for long_ in (True, False):
            lv = exit_levels(ref, long_, Decimal("0.1"))
            assert lv.scale == 1
            assert lv == legacy_exit_levels(ref, long_), "unchanged where the old levels were valid"
    for ref in (Decimal("1000"), Decimal("1999.9")):  # LONG offsets valid across the walk range
        assert exit_levels(ref, True, Decimal("0.1")) == legacy_exit_levels(ref, True)


def test_low_short_is_scaled_not_clamped_flipped_or_skipped():
    lv = exit_levels(Decimal("1000"), False, Decimal("0.1"))
    assert lv.scale < 1 and lv.take_profits[1] == Decimal("500")  # exactly the 50% bound
    assert lv.stop == Decimal("1000") + floor_to_step(BASE_OFFSETS[0] * lv.scale, Decimal("0.1"))
    assert lv.take_profits[0] not in (Decimal(0), Decimal("1000"))


def test_degenerate_levels_raise_instead_of_clamping():
    with pytest.raises(SoakWorkloadError) as ei:
        exit_levels(Decimal("0.3"), False, Decimal("0.1"))
    assert ei.value.code == "LEVELS_DEGENERATE"
    with pytest.raises(SoakWorkloadError):
        exit_levels(Decimal("1000.05"), True, Decimal("0.1"))  # reference off the tick grid
    with pytest.raises(SoakWorkloadError):
        exit_levels(Decimal("0"), True, Decimal("0.1"))


def test_common_tick_is_the_least_common_multiple():
    assert common_tick(VENUE_TICKS) == Decimal("0.1")
    assert common_tick([Decimal("0.25"), Decimal("0.1")]) == Decimal("0.5")
    assert common_tick([Decimal("1"), Decimal("0.5")]) == Decimal("1")
    with pytest.raises(SoakWorkloadError):
        common_tick([])


def test_contract_still_rejects_the_legacy_invalid_short():
    """The historical defect reproduced at the contract: NOT loosened."""
    lv = legacy_exit_levels(Decimal("1010"), False)
    assert lv.take_profits[1] == Decimal("-490")
    ev = build_soak_open(NOW, "soak-x01", Decimal("1010"), False, lv, max_holding_seconds=30)
    with pytest.raises(Rejection) as ei:
        parse_event(load_event_json(json.dumps(ev)))
    assert str(ei.value.code) == "INVALID_NUMBER" and "take_profits[1].price" in str(ei.value)
    with pytest.raises(SoakWorkloadError) as gi:
        validate_generated(ev, VENUE_TICKS)
    assert gi.value.code == "INVALID_NUMBER"
    zero = build_soak_open(NOW, "soak-x02", Decimal("1500"), False,
                           legacy_exit_levels(Decimal("1500"), False), max_holding_seconds=30)
    with pytest.raises(SoakWorkloadError):
        validate_generated(zero, VENUE_TICKS)  # TP2 == 0 is not > 0


def test_tick_misaligned_price_is_a_generator_error():
    ev = build_open(NOW, "soak-x03", direction="LONG", reference_entry="64500.1", stop="64000.07",
                    take_profits=[("65300.1", "0.5"), ("66000.1", "0.5")])
    with pytest.raises(SoakWorkloadError) as ei:
        validate_generated(ev, [Decimal("0.05")])
    assert ei.value.code == "TICK_MISALIGNED"


def test_walk_consumes_the_rng_exactly_like_the_phase2b_loop():
    """Old inline loop (Phase 2A/2B soak.py) vs MarketWalk: same prices and directions."""
    rng = random.Random(11)
    price = Decimal("64500.0")
    walk = MarketWalk(11, Decimal("64500.0"))
    for n in range(6000):
        step = Decimal(rng.choice([-40, -25, -10, 0, 10, 25, 40]))
        if rng.random() < 0.06:
            step = Decimal(rng.choice([-900, 900, 1600, -1600]))
        price = max(Decimal("1000"), floor_to_step(price + step, Decimal("0.1")))
        assert walk.advance() == price
        if n % 8 == 0:
            assert walk.draw_long() == (rng.random() < 0.5)


def test_seeded_historical_replay_reproduces_90_then_zero_malformed():
    legacy = replay(**HIST, ticks=VENUE_TICKS, legacy=True)
    assert legacy["signals"] == 4088 and legacy["invalid"] == 90
    assert legacy["invalid_by_code"] == {"INVALID_NUMBER": 90}
    ids = legacy["invalid_trade_ids"]
    assert ids[0] == "soak-025992" and ids[-1] == "soak-032496"  # = historical quarantine files
    fixed = replay(**HIST, ticks=VENUE_TICKS)
    assert fixed["signals"] == 4088 and fixed["valid"] == 4088 and fixed["invalid"] == 0
    assert fixed["by_direction"] == legacy["by_direction"], "no direction changed"
    assert fixed["price_min"] == "1000" and fixed["scaled"] > 0
    assert replay(**HIST, ticks=VENUE_TICKS) == fixed, "deterministic"


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 42])
def test_other_seeds_beyond_historical_length_have_zero_malformed(seed):
    r = replay(seed=seed, cycles=40000, trade_every_cycles=8, initial_price=Decimal("64500.0"),
               ticks=VENUE_TICKS)
    assert r["invalid"] == 0 and r["valid"] == r["signals"] == 5000


# ------------------------------------------------------------------ soak end-to-end
def test_low_price_soak_publishes_only_valid_signals(tmp_path):
    cfg = _cfg(tmp_path, **{'initial_price = "64500.0"': 'initial_price = "1100.0"'})
    rep = run_soak(cfg, tmp_path / "low", cycles=320, accelerated=True, poll_seconds=1.0,
                   funding_profile="funded-test-100k")
    gen = rep["workload_generator"]
    assert gen["generator_errors"] == 0 and gen["signals_scaled"] > 0
    assert rep["ingest_outcomes"] == {"JOURNALED": rep["signals_published"]}
    assert rep["workload_acceptance"]["ok"], rep["workload_acceptance"]
    assert rep["reconciliation"]["all_reconcile"], rep["reconciliation"]["checks"]
    assert rep["invariant_violations_total"] == 0


def test_generator_error_is_counted_never_published(tmp_path, monkeypatch, capsys):
    real = soak_mod.exit_levels
    calls = {"n": 0}

    def flaky(ref, long_, tick):
        calls["n"] += 1
        if calls["n"] == 3:
            raise SoakWorkloadError("LEVELS_DEGENERATE", "injected harness defect")
        return real(ref, long_, tick)

    monkeypatch.setattr(soak_mod, "exit_levels", flaky)
    cfg = _cfg(tmp_path)
    root = tmp_path / "gen"
    rc = main(["--config", str(cfg), "--data-dir", str(root), "soak", "--cycles", "40",
               "--accelerated", "--poll-seconds", "1", "--funding-profile", "funded-test-100k"])
    rep = json.loads((root / "soak-report.json").read_text(encoding="utf-8"))
    assert rc == 1, "a harness error fails visibly"
    assert rep["workload_generator"]["generator_errors"] == 1
    assert rep["workload_generator"]["signals_generated"] == 5
    assert rep["signals_published"] == 4 and rep["ingest_outcomes"] == {"JOURNALED": 4}
    assert not any("soak-000016" in p.name for p in (root / "archive").iterdir())
    assert not rep["workload_acceptance"]["ok"] and not rep["workload_acceptance"]["workload_clean"]
    assert rep["reconciliation"]["checks"]["generated == published + generator_errors"]
    assert "REVIEW REQUIRED" in capsys.readouterr().out


def test_funded_profile_changes_only_synthetic_wallets(tmp_path):
    cfg = _cfg(tmp_path)
    rep = run_soak(cfg, tmp_path / "funded", cycles=400, accelerated=True, poll_seconds=1.0,
                   funding_profile="funded-test-100k")
    ident = json.loads((tmp_path / "funded" / "soak-identity.json").read_text(encoding="utf-8"))
    f = ident["funding"]
    assert f["profile"] == "funded-test-100k" and f["synthetic"] and f["replenishment"] == "none"
    assert f["starting_balances"] == {"sim_alpha_a1": "100000.00", "sim_beta_b1": "100000.00"}
    assert f["configured_starting_balances"] == {"sim_alpha_a1": "1000.00", "sim_beta_b1": "1000.00"}
    assert f["config_source_hash"] == ident["runtime"]["config"]["source_hash"]
    pol = ident["effective_policies"]["accounts"]["sim_alpha_a1"]
    assert (pol["initial_margin_per_trade"], pol["max_notional_hard"], pol["max_initial_margin_hard"],
            pol["collateral_buffer_fraction"]) == ("100.00", "600.00", "110.00", "0.05")
    assert ident["parameters"]["seed"] == 7 and "git_head" in ident["runtime"]
    assert "INSUFFICIENT_COLLATERAL" not in rep["rejections"]["expected"]
    assert rep["rejections"]["unexpected_review"] == {}, rep["rejections"]
    assert rep["execution_states"].get("CLOSED", 0) > 10
    for aid, b in rep["balances"].items():
        assert b["starting"] == "100000.00" and b["ledger_reconciles"], (aid, b)
        assert Decimal(b["fees"]) < 0 and b["other_ledger"] == {}, "fees charged; no replenishment"
    q = rep["coverage_by_quarter"]
    assert len(q) == 4 and all(sum(a["closed"] for a in x["accounts"].values()) > 0 for x in q)
    assert rep["workload_acceptance"]["ok"] and rep["reconciliation"]["all_reconcile"]


def test_low_capital_profile_still_refuses_with_insufficient_collateral(tmp_path):
    cfg = _cfg(tmp_path)
    rep = run_soak(cfg, tmp_path / "poor", cycles=48, accelerated=True, poll_seconds=1.0,
                   funding_profile="low-capital-test-50")
    assert rep["execution_states"] == {"REJECTED": 2 * rep["signals_published"]}
    assert rep["rejections"]["expected"] == {"INSUFFICIENT_COLLATERAL": 2 * rep["signals_published"]}
    assert rep["reconciliation"]["simulator"]["orders"] == 0, "no order reached the venue"
    for b in rep["balances"].values():
        assert b["starting"] == b["ending_wallet"] == "50.00"


def test_collateral_rejection_is_never_expected_in_a_funded_profile():
    exp = FUNDING_PROFILES["funded-test-100k"]["expected_rejections"]
    assert set(exp) == {"OVERLAPPING_TRADE"}
    assert set(FUNDING_PROFILES["config"]["expected_rejections"]) == {"OVERLAPPING_TRADE"}

    class J:
        def q(self, sql, args=()):
            if "FROM executions WHERE state='REJECTED'" in sql:
                return [{"code": "INSUFFICIENT_COLLATERAL", "n": 3}, {"code": "OVERLAPPING_TRADE", "n": 2}]
            return []

        def open_incidents(self):
            return []

    r = _journal_summary(J(), exp)["rejections"]
    assert r["unexpected_review"] == {"INSUFFICIENT_COLLATERAL": 3}
    assert r["expected"] == {"OVERLAPPING_TRADE": 2}


def test_unknown_funding_profile_is_refused_before_any_file(tmp_path):
    with pytest.raises(ValueError):
        run_soak(_cfg(tmp_path), tmp_path / "x", cycles=1, accelerated=True, funding_profile="rich")
    assert not (tmp_path / "x").exists()


# ------------------------------------------------------------------ negative: external input
def test_external_nonpositive_price_signal_is_still_quarantined_strict(tmp_path, monkeypatch):
    key = "cd" * 32  # TEST-ONLY key material
    monkeypatch.setenv("ASEKE_EXEC_HMAC_SIM_DEMO_K1", key)
    cfg = _cfg(tmp_path, "sim-strict.toml")
    root = tmp_path / "neg"
    assert main(["--config", str(cfg), "--data-dir", str(root), "init-runtime"]) == 0
    now = SystemClock().now()
    lv = legacy_exit_levels(Decimal("1010"), False)
    ev = build_soak_open(now, "neg-short-1", Decimal("1010"), False, lv, max_holding_seconds=30,
                         producer="aseke-dummy-publisher")
    env = build_envelope(ev, key=bytes.fromhex(key), key_id="sim-demo-k1",
                         audience="aseke-exec:sim-local", issued_at=now)
    publish_event(root / "inbox", env, filename=signal_filename(ev))
    assert main(["--config", str(cfg), "--data-dir", str(root), "cycle"]) == 0
    c = sqlite3.connect((root / "journal.db").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = c.execute("SELECT outcome, code FROM ingest_log").fetchall()
        n_exec = c.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
    finally:
        c.close()
    assert rows == [("INVALID", "INVALID_NUMBER")] and n_exec == 0
    reasons = list((root / "quarantine").glob("*.reason.json"))
    assert len(reasons) == 1 and "take_profits[1].price" in reasons[0].read_text(encoding="utf-8")


# ------------------------------------------------------------------ review follow-ups
def test_coverage_quarter_labels_match_bucketing_for_uneven_spans():
    from aseke_exec.soak import _coverage

    class J:
        def __init__(self, n):
            self.rows = [{"trade_id": f"soak-{c:06d}", "account_id": "a", "state": "CLOSED",
                          "reason_code": None} for c in range(n)]

        def q(self, sql, args=()):
            return self.rows

    for span in (10, 11, 13, 32702):
        q = _coverage(J(span), span)
        assert q[0]["cycles"][0] == 0 and q[-1]["cycles"][1] == span - 1
        for x in q:
            lo, hi = x["cycles"]
            assert x["accounts"]["a"]["closed"] == hi - lo + 1, (span, x)
        assert sum(x["accounts"]["a"]["closed"] for x in q) == span


def test_final_pass_does_not_double_count_a_known_violation(tmp_path, monkeypatch):
    monkeypatch.setattr(soak_mod, "check_invariants", lambda rt, checked=None: ["synthetic violation"])
    rep = run_soak(_cfg(tmp_path), tmp_path / "dup", cycles=5, accelerated=True, poll_seconds=1.0,
                   funding_profile="funded-test-100k")
    assert rep["invariant_violations_total"] == 5, "one per cycle; the final pass adds nothing new"
    assert not any(v.startswith("final:") for v in rep["invariant_violations"])
    assert not rep["workload_acceptance"]["ok"]


def test_unexpected_collateral_refusal_under_config_profile_exits_1(tmp_path, capsys):
    cfg = _cfg(tmp_path, **{'starting_balance = "1000.00"': 'starting_balance = "50.00"'})
    root = tmp_path / "cfg-poor"
    rc = main(["--config", str(cfg), "--data-dir", str(root), "soak", "--cycles", "24",
               "--accelerated", "--poll-seconds", "1"])
    rep = json.loads((root / "soak-report.json").read_text(encoding="utf-8"))
    assert rep["rejections"]["unexpected_review"].get("INSUFFICIENT_COLLATERAL", 0) > 0
    assert rep["invariant_violations_total"] == 0 and rc == 1, "reported for review, not relabelled"
    assert "REVIEW REQUIRED" in capsys.readouterr().out


def test_ledger_and_reconciliation_detect_tampering(tmp_path):
    from aseke_exec.config import load_config
    from aseke_exec.runtime import build_runtime
    from aseke_exec.soak import _balances, _reconciliation
    cfg = load_config(_cfg(tmp_path), data_dir=tmp_path / "t")
    rt = build_runtime(cfg, SystemClock())
    try:
        assert all(b["ledger_reconciles"] for b in _balances(rt).values())
        rt.sim.set_wallet("sim_alpha", "sim_alpha_a1", "1234.00")  # change without a ledger row
        assert not _balances(rt)["sim_alpha_a1"]["ledger_reconciles"]
        assert _reconciliation(rt, 0, 0, 0)["all_reconcile"]
        bad = _reconciliation(rt, 3, 0, 2)  # one generated signal unaccounted for
        assert not bad["all_reconcile"]
        assert not bad["checks"]["generated == published + generator_errors"]
    finally:
        rt.close()
