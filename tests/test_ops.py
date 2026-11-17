"""Operator safety: runtime fingerprint, health, control queue, reset safety, fresh runtime
init, coordinated backup/restore to a NEW root, and the accelerated soak harness."""

import json
import logging
import shutil
from pathlib import Path

import pytest

from aseke_exec.cli import main
from aseke_exec.config import load_config
from aseke_exec.lock import ProcessLock
from aseke_exec.ops import wal_reset_status
from aseke_exec.soak import run_soak

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _quiet():
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def cfgfile(tmp_path):
    p = tmp_path / "sim.toml"
    shutil.copy(ROOT / "config/sim.toml", p)
    return p


def run(cfgfile, root, *args):
    return main(["--config", str(cfgfile), "--data-dir", str(root), *args])


@pytest.mark.parametrize("v,status", [("3.49.1", "AFFECTED"), ("3.51.2", "AFFECTED"),
                                      ("3.51.3", "FIXED"), ("3.52.0", "FIXED"),
                                      ("3.50.7", "FIXED_BACKPORT"), ("3.50.6", "AFFECTED"),
                                      ("3.44.6", "FIXED_BACKPORT"), ("3.44.5", "AFFECTED")])
def test_wal_reset_bug_classification(v, status):
    assert wal_reset_status(v)["wal_reset_bug"] == status


def test_runtime_info_has_versions_and_no_secret_values(cfgfile, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ASEKE_EXEC_HMAC_SIM_DEMO_K1", "ab" * 32)
    strict = tmp_path / "strict.toml"
    shutil.copy(ROOT / "config/sim-strict.toml", strict)
    assert main(["--config", str(strict), "--data-dir", str(tmp_path / "r"), "runtime-info"]) == 0
    out = capsys.readouterr().out
    info = json.loads(out)
    for k in ("python_version", "sqlite_version", "sqlite_source_id", "wal_reset_bug",
              "source_fingerprint_sha256", "dependencies"):
        assert info[k] is not None
    assert "ab" * 32 not in out, "secret value never reported"
    assert not (tmp_path / "r").exists(), "read-only command created no runtime state"


def test_fresh_runtime_and_reset_safety(cfgfile, tmp_path, capsys):
    root = tmp_path / "rt"
    assert run(cfgfile, root, "init-runtime") == 0
    assert run(cfgfile, root, "init-runtime") == 2, "never re-initialise a non-empty root"
    assert run(cfgfile, root, "sim", "init", "--reset") == 0, "allowed: journal has no history"
    assert run(cfgfile, root, "publish-example", "long", "--trade-id", "ops-1") == 0
    assert run(cfgfile, root, "cycle") == 0
    capsys.readouterr()
    assert run(cfgfile, root, "sim", "init", "--reset") == 2
    assert "inconsistent" in capsys.readouterr().out
    with ProcessLock(root / "worker.lock"):
        assert run(cfgfile, root, "sim", "init", "--reset") == 2, "never reset an active runtime"


def test_health_distinguishes_process_from_lifecycle(cfgfile, tmp_path, capsys):
    root = tmp_path / "rt"
    run(cfgfile, root, "init-runtime")
    run(cfgfile, root, "publish-example", "long", "--trade-id", "ops-h")
    run(cfgfile, root, "cycle")
    capsys.readouterr()
    rc = run(cfgfile, root, "health", "--json")
    rep = json.loads(capsys.readouterr().out)
    # a one-shot `cycle` is NOT a running worker, even with a fresh heartbeat
    assert rep["process"]["running"] is False and rep["process"]["heartbeat_kind"] == "one-shot"
    assert rc == 1 and any("no resident worker" in r for r in rep["lifecycle"]["reasons"])
    ex = rep["active_executions"]
    assert {e["stop_coverage"] for e in ex} == {"VERIFIED"} and {e["tp_coverage"] for e in ex} == {"COMPLETE"}
    assert {a["account_id"]: a["new_entry_gate"] for a in rep["accounts"]}["sim_alpha_a1"] == ["open"]
    assert rep["data_dir"] == str(root.resolve())
    if rep["runtime"]["wal_reset_bug"] == "AFFECTED":
        assert rc == 1 and any("WAL-reset" in r for r in rep["lifecycle"]["reasons"])
    for key in ("quote_age_seconds", "unresolved_orders", "open_incidents", "pending_events"):
        assert key in rep


def test_operator_command_is_queued_while_worker_runs(cfgfile, tmp_path, capsys):
    root = tmp_path / "rt"
    run(cfgfile, root, "init-runtime")
    with ProcessLock(root / "worker.lock"):  # simulate a running worker
        assert run(cfgfile, root, "pause", "--reason", "maint") == 0
        assert "QUEUED" in capsys.readouterr().out
        assert len(list((root / "control").glob("*.json"))) == 1
    assert run(cfgfile, root, "cycle") == 0  # the worker applies queued requests
    assert not list((root / "control").glob("*.json"))
    capsys.readouterr()
    run(cfgfile, root, "status", "--json")
    assert json.loads(capsys.readouterr().out)["controls"]["pause_new_entries"] == "1"


def test_coordinated_backup_and_restore_to_new_root(cfgfile, tmp_path, capsys):
    root, bk, new = tmp_path / "rt", tmp_path / "backup1", tmp_path / "restored"
    run(cfgfile, root, "init-runtime")
    run(cfgfile, root, "publish-example", "long", "--trade-id", "ops-bk")
    run(cfgfile, root, "cycle")
    src_before = (root / "journal.db").stat().st_size
    with ProcessLock(root / "worker.lock"):
        assert run(cfgfile, root, "backup", "--dest", str(bk)) == 3, "refused while a worker runs"
    capsys.readouterr()
    assert run(cfgfile, root, "backup", "--dest", str(bk)) == 0
    man = json.loads((bk / "manifest.json").read_text())
    assert man["quiesced"] and man["generation"] and man["row_counts"]["journal.db"]["executions"] == 2
    assert (root / "journal.db").stat().st_size == src_before, "source runtime untouched"
    assert run(cfgfile, root, "backup", "--dest", str(bk)) == 2, "never overwrite a backup"
    assert run(cfgfile, root, "restore", "--backup", str(bk)) == 2, "never restore over the live root"
    assert main(["--config", str(cfgfile), "restore", "--backup", str(bk)]) == 2, "no implicit target"
    assert run(cfgfile, new, "restore", "--backup", str(bk)) == 0
    assert run(cfgfile, new, "restore", "--backup", str(bk)) == 2, "never restore over a runtime"
    # tamper detection
    bad = tmp_path / "bad"
    shutil.copytree(bk, bad)
    with open(bad / "journal.db", "r+b") as fh:
        fh.seek(200)
        fh.write(b"\x00\x01")
    assert run(cfgfile, tmp_path / "r2", "restore", "--backup", str(bad)) == 2
    # restored runtime reconciles before entries and keeps the protected positions
    capsys.readouterr()
    assert run(cfgfile, new, "cycle") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reconcile"] == {"sim_alpha_a1": "OK", "sim_beta_b1": "OK"}
    run(cfgfile, new, "status", "--json")
    rep = json.loads(capsys.readouterr().out)
    assert {e["state"] for e in rep["active_executions"]} == {"OPEN_PROTECTED"}
    assert (new / "restored-from.json").exists()


def test_backup_manifest_records_the_source_schema_not_the_code_schema(cfgfile, tmp_path, capsys):
    import sqlite3
    root, bk, new = tmp_path / "rt", tmp_path / "bk", tmp_path / "new"
    run(cfgfile, root, "init-runtime")
    run(cfgfile, root, "publish-example", "long", "--trade-id", "ops-v1")
    run(cfgfile, root, "cycle")
    c = sqlite3.connect(root / "journal.db")  # label the journal as an older schema (v1)
    c.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    c.commit()
    c.close()
    assert run(cfgfile, root, "backup", "--dest", str(bk)) == 0
    assert json.loads((bk / "manifest.json").read_text())["journal_schema_version"] == 1
    assert run(cfgfile, new, "restore", "--backup", str(bk)) == 0
    assert run(cfgfile, new, "cycle") == 0
    ro = sqlite3.connect((new / "journal.db").resolve().as_uri() + "?mode=ro", uri=True)
    assert ro.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    ro.close()
    src = sqlite3.connect((root / "journal.db").resolve().as_uri() + "?mode=ro", uri=True)
    assert src.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1", \
        "backup never migrates the source root"
    src.close()


def test_accelerated_soak_runs_with_no_invariant_violations(cfgfile, tmp_path):
    logging.disable(logging.WARNING)
    rep = run_soak(cfgfile, tmp_path / "soak", cycles=400, accelerated=True, poll_seconds=1.0)
    assert rep["mode"].startswith("accelerated") and rep["cycles"] == 400
    assert rep["invariant_violations"] == [], rep["invariant_violations"][:5]
    assert rep["execution_states"].get("CLOSED", 0) > 5, rep["execution_states"]
    with pytest.raises(FileExistsError):
        run_soak(cfgfile, tmp_path / "soak", cycles=1, accelerated=True)


def test_missing_auth_profile_is_a_config_error(tmp_path):
    text = (ROOT / "config/sim.toml").read_text(encoding="utf-8")
    start = text.index("[auth]")
    end = text.index("# ----", start)
    p = tmp_path / "noauth.toml"
    p.write_text(text[:start] + text[end:], encoding="utf-8")
    from aseke_exec.config import ConfigError
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert "auth.mode must be set explicitly" in str(ei.value)


def test_malformed_control_requests_are_set_aside_without_stalling_cycles(cfgfile, tmp_path, capsys):
    root = tmp_path / "rt"
    run(cfgfile, root, "init-runtime")
    (root / "control" / "a_list.json").write_text("[1, 2]", encoding="utf-8")
    (root / "control" / "b_noid.json").write_text('{"action": "incident-resolve"}', encoding="utf-8")
    (root / "control" / "c_bad.json").write_text('{"action": "incident-resolve", "target": "x"}',
                                                 encoding="utf-8")
    run(cfgfile, root, "publish-example", "long", "--trade-id", "ops-ctl")
    assert run(cfgfile, root, "cycle") == 0
    assert sorted(p.name for p in (root / "control" / "rejected").iterdir()) == \
        ["a_list.json", "b_noid.json", "c_bad.json"]
    capsys.readouterr()
    run(cfgfile, root, "status", "--json")
    rep = json.loads(capsys.readouterr().out)
    assert {e["state"] for e in rep["active_executions"]} == {"OPEN_PROTECTED"}, "cycle not stalled"
