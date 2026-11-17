"""Phase 2B runtime policy: numeric SQLite classification, release exclusions, approved-build
matching, and the startup guard (refusal happens BEFORE any runtime file or database exists)."""

import json
import logging
import shutil
from pathlib import Path

import pytest

from aseke_exec import runtime_policy as rp
from aseke_exec.cli import main
from aseke_exec.journal import Journal

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("v,status", [
    ("3.7.0", "AFFECTED"), ("3.44.5", "AFFECTED"), ("3.44.6", "FIXED_BACKPORT"), ("3.44.9", "FIXED_BACKPORT"),
    ("3.45.0", "AFFECTED"), ("3.49.1", "AFFECTED"), ("3.50.4", "AFFECTED"), ("3.50.6", "AFFECTED"),
    ("3.50.7", "FIXED_BACKPORT"), ("3.50.10", "FIXED_BACKPORT"), ("3.51.0", "AFFECTED"),
    ("3.51.2", "AFFECTED"), ("3.51.3", "FIXED"), ("3.52.0", "FIXED"), ("3.53.4", "FIXED"),
    ("3.53.4.0", "FIXED"), ("3.100.0", "FIXED"), ("3.6.23", "NOT_APPLICABLE"),
    ("", "UNKNOWN"), ("3.53", "UNKNOWN"), ("3.53.x", "UNKNOWN"), ("v3.53.4", "UNKNOWN"), (None, "UNKNOWN"),
])
def test_wal_reset_classification_is_numeric(v, status):
    assert rp.classify_wal_reset(v) == status


def test_release_policy_exclusions_are_separate_from_the_fix():
    assert rp.classify_wal_reset("3.52.0") == "FIXED" and "withdrawn" in rp.release_exclusion("3.52.0")
    assert rp.release_exclusion("3.54.0") and rp.release_exclusion("3.54.1") is None
    assert rp.release_exclusion("3.53.4") is None and rp.release_exclusion("garbage") is None


def _lock() -> dict:
    return json.loads((ROOT / "config/runtime-lock.json").read_text(encoding="utf-8"))


def _probe(**over) -> dict:
    lock = _lock()
    p = {"implementation": "cpython", "python_version": lock["python"]["version"], "pointer_bits": 64,
         "free_threaded": False, "sqlite_version": lock["sqlite"]["version"],
         "sqlite_module_version": lock["sqlite"]["version"], "sqlite_source_id": lock["sqlite"]["source_id"],
         "base_prefix": str(ROOT / lock["paths"]["interpreter_dir"]), "executable": "x"}
    p.update(over)
    return p


def test_exact_pinned_build_is_operational_and_soak_evidence_is_separate():
    st = rp.evaluate(_probe(), _lock(), ROOT)
    assert st.operational and st.approved_build and st.wal_reset_fix == "FIXED" and not st.reasons
    assert st.extended_soak == "PENDING", "a version check never marks soak evidence complete"


@pytest.mark.parametrize("over,needle", [
    ({"sqlite_version": "3.49.1", "sqlite_module_version": "3.49.1"}, "AFFECTED"),
    ({"sqlite_source_id": "2026-07-24 19:02:57 0000"}, "source id"),
    ({"python_version": "3.12.10"}, "Python version"),
    ({"pointer_bits": 32}, "64-bit"),
    ({"free_threaded": True}, "free-threading"),
    ({"implementation": "pypy"}, "implementation"),
    ({"base_prefix": r"C:\Users\someone\AppData\Local\Programs\Python\Python315"}, "project-private"),
    ({"sqlite_version": "3.52.0", "sqlite_module_version": "3.52.0"}, "release policy"),
    ({"sqlite_version": "weird", "sqlite_module_version": "weird"}, "UNKNOWN"),
    ({"sqlite_module_version": "3.53.3"}, "differs"),
])
def test_any_mismatch_or_unknown_is_not_operational(over, needle):
    st = rp.evaluate(_probe(**over), _lock(), ROOT)
    assert not st.operational
    assert any(needle in r for r in st.reasons), st.reasons


def test_patched_but_unpinned_build_is_fixed_yet_not_approved():
    st = rp.evaluate(_probe(sqlite_version="3.54.1", sqlite_module_version="3.54.1",
                            sqlite_source_id="other"), _lock(), ROOT)
    assert st.wal_reset_fix == "FIXED" and not st.approved_build and not st.operational


def test_missing_lock_is_never_approved():
    st = rp.evaluate(_probe(), None, ROOT)
    assert not st.operational and st.extended_soak == "UNKNOWN"


def test_this_test_process_runs_on_the_approved_runtime():
    st = rp.current()
    assert st.operational, st.reasons  # the suite itself must run on .venv-exec


# ------------------------------------------------------------------ guard (simulated refusal)
@pytest.fixture
def unapproved(monkeypatch):
    bad = rp.evaluate(_probe(sqlite_version="3.49.1", sqlite_module_version="3.49.1"), _lock(), ROOT)
    monkeypatch.setattr(rp, "current", lambda: bad)
    return bad


@pytest.fixture
def cfgfile(tmp_path):
    p = tmp_path / "sim.toml"
    shutil.copy(ROOT / "config/sim.toml", p)
    return p


@pytest.mark.parametrize("cmd", [["cycle"], ["init-runtime"], ["run", "--max-cycles", "1"], ["reconcile"],
                                 ["publish-example", "long"], ["pause"], ["account", "disable", "sim_alpha_a1"],
                                 ["sim", "init"], ["sim", "show"], ["backup", "--dest", "bk"],
                                 ["soak", "--cycles", "1", "--accelerated"], ["scenario", "run", "all"],
                                 ["auth", "sign", "--in", "x.json"]])
def test_operational_commands_refused_before_any_runtime_file(unapproved, cfgfile, tmp_path, capsys, cmd):
    root = tmp_path / "rt"
    rc = main(["--config", str(cfgfile), "--data-dir", str(root), *cmd])
    out = capsys.readouterr().out
    assert rc == 4 and out.startswith("refused:"), out
    assert "AFFECTED" in out and "3.49.1" in out
    assert not root.exists(), "nothing created on an unapproved runtime"
    assert not (tmp_path / "bk").exists()


def test_diagnostics_stay_usable_on_an_unapproved_runtime(unapproved, cfgfile, tmp_path, capsys):
    root = tmp_path / "rt"
    assert main(["--config", str(cfgfile), "--data-dir", str(root), "validate-config"]) == 0
    assert main(["--config", str(cfgfile), "--data-dir", str(root), "auth", "check"]) == 0
    assert main(["scenario", "list"]) == 0
    capsys.readouterr()
    assert main(["--config", str(cfgfile), "--data-dir", str(root), "runtime-info"]) == 0
    assert not root.exists()


def test_library_entries_refuse_too(unapproved, cfgfile, tmp_path):
    from aseke_exec.config import load_config
    from aseke_exec.runtime import run_worker
    from aseke_exec.soak import run_soak
    with pytest.raises(rp.RuntimeNotApproved):
        Journal(tmp_path / "j.db")
    assert not (tmp_path / "j.db").exists()
    with pytest.raises(rp.RuntimeNotApproved):
        run_worker(load_config(cfgfile, data_dir=tmp_path / "w"), max_cycles=1)
    assert not (tmp_path / "w" / "worker.lock").exists()
    logging.disable(logging.WARNING)
    with pytest.raises(rp.RuntimeNotApproved):
        run_soak(cfgfile, tmp_path / "s", cycles=1, accelerated=True)
    assert not (tmp_path / "s").exists()
    from aseke_exec.clock import SystemClock
    from aseke_exec.sim import SimExchange
    with pytest.raises(rp.RuntimeNotApproved):
        SimExchange(tmp_path / "simdir" / "sim_exchange.db", SystemClock())
    assert not (tmp_path / "simdir").exists(), "refused before any directory/file is created"


def test_read_only_inspection_still_works_when_unapproved(cfgfile, tmp_path, capsys, monkeypatch):
    root = tmp_path / "rt"
    assert main(["--config", str(cfgfile), "--data-dir", str(root), "init-runtime"]) == 0  # approved
    bad = rp.evaluate(_probe(sqlite_version="3.49.1", sqlite_module_version="3.49.1"), _lock(), ROOT)
    monkeypatch.setattr(rp, "current", lambda: bad)
    capsys.readouterr()
    assert main(["--config", str(cfgfile), "--data-dir", str(root), "status", "--json"]) == 0
    ro = Journal(root / "journal.db", readonly=True)
    try:
        assert ro.schema_version() >= 2
    finally:
        ro.close()
    assert main(["--config", str(cfgfile), "--data-dir", str(root), "cycle"]) == 4


def test_no_environment_variable_bypass(monkeypatch):
    for name in ("ASEKE_EXEC_ALLOW_UNAPPROVED_RUNTIME", "ASEKE_EXEC_RUNTIME_OVERRIDE", "ASEKE_EXEC_UNSAFE"):
        monkeypatch.setenv(name, "1")
    st = rp.evaluate(_probe(sqlite_version="3.49.1", sqlite_module_version="3.49.1"), _lock(), ROOT)
    assert not st.operational
    src = (ROOT / "src/aseke_exec/runtime_policy.py").read_text(encoding="utf-8")
    assert "os.environ" not in src and "getenv" not in src, "policy must not read an override"
