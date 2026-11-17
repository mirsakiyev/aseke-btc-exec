"""Phase 2B: the canonical interpreter, wrappers, setup idempotence/interruption, storage
compatibility and the read-only-after-crash behaviour of the patched SQLite.

Subprocess tests only kill processes they start. Setup tests run in a TEMPORARY copy of the
repository (path with spaces) using the already verified local artifacts (no network)."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aseke_exec import runtime_policy as rp
from aseke_exec.journal import Journal, connect_readonly

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "config/runtime-lock.json").read_text(encoding="utf-8"))
WRAPPER = ROOT / "scripts" / "aseke-exec.ps1"
OLD_PY = ROOT / ".venv" / "Scripts" / "python.exe"
WINPS = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
ZIP = ROOT / LOCK["paths"]["downloads_dir"] / LOCK["artifact"]["name"]
WHEELS = ROOT / LOCK["paths"]["wheel_dir"]

windows_only = pytest.mark.skipif(os.name != "nt" or not WINPS.exists(), reason="Windows PowerShell required")


def _ps(script: Path, *args: str, cwd: Path, env: dict | None = None, timeout: int = 300):
    return subprocess.run([str(WINPS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
                          capture_output=True, text=True, cwd=str(cwd), env=env, timeout=timeout)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------ interpreter identity
def test_candidate_and_effective_interpreter_agree_including_subprocesses():
    me = rp.current()
    assert me.operational, me.reasons
    venv = (ROOT / LOCK["paths"]["venv_dir"]).resolve()
    assert Path(sys.prefix).resolve() == venv, "tests must run in the approved .venv-exec"
    r = subprocess.run([sys.executable, "-I", "-m", "aseke_exec", "runtime-info"], capture_output=True,
                       text=True, cwd=str(ROOT), timeout=60)
    child = json.loads(r.stdout)
    assert r.returncode == 0 and child["runtime_policy"]["operational"] is True
    assert Path(child["python_executable"]).resolve() == Path(sys.executable).resolve()
    assert child["sqlite_source_id"] == me.probe["sqlite_source_id"] == LOCK["sqlite"]["source_id"]
    assert child["sqlite_version"] == sqlite3.sqlite_version == LOCK["sqlite"]["version"]
    assert Path(child["interpreter"]["_sqlite3_module"]).resolve().is_relative_to(
        (ROOT / LOCK["paths"]["interpreter_dir"]).resolve()), "loaded _sqlite3 comes from the project runtime"


@pytest.mark.skipif(not OLD_PY.exists(), reason="old .venv not present")
def test_old_affected_interpreter_cannot_bypass_the_guard(tmp_path):
    root = tmp_path / "old rt"
    for cmd in (["cycle"], ["init-runtime"], ["run", "--max-cycles", "1"], ["sim", "init"],
                ["publish-example", "long"]):
        r = subprocess.run([str(OLD_PY), "-m", "aseke_exec", "--data-dir", str(root), *cmd],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        assert r.returncode == 4 and "refused" in r.stdout and "AFFECTED" in r.stdout, (cmd, r.stdout, r.stderr)
    assert not root.exists(), "old interpreter created no runtime state"
    info = subprocess.run([str(OLD_PY), "-m", "aseke_exec", "runtime-info"], capture_output=True, text=True,
                          cwd=str(ROOT), timeout=60)
    d = json.loads(info.stdout)
    assert info.returncode == 0 and d["runtime_policy"]["operational"] is False
    assert d["wal_reset_bug"] == "AFFECTED", "diagnostics remain available on the old runtime"


# ------------------------------------------------------------------ wrapper
@windows_only
def test_wrapper_from_other_cwd_with_spaces_and_explicit_data_dir(tmp_path):
    cwd = tmp_path / "a dir with spaces"
    cwd.mkdir()
    rt = tmp_path / "runtime root with spaces" / "rt 1"
    assert _ps(WRAPPER, "--data-dir", str(rt), "init-runtime", cwd=cwd).returncode == 0
    assert _ps(WRAPPER, "--data-dir", str(rt), "publish-example", "long", "--trade-id", "w-1", cwd=cwd).returncode == 0
    assert _ps(WRAPPER, "--data-dir", str(rt), "cycle", cwd=cwd).returncode == 0
    st = _ps(WRAPPER, "--data-dir", str(rt), "status", "--json", cwd=cwd)
    assert st.returncode == 0
    ex = json.loads(st.stdout)["active_executions"]
    assert {e["state"] for e in ex if e["trade_id"] == "w-1"} == {"OPEN_PROTECTED"}
    assert (rt / "journal.db").exists() and not (cwd / "runtime").exists(), "--data-dir honoured"
    # exit codes propagate (refusal = 2)
    assert _ps(WRAPPER, "--data-dir", str(tmp_path / "new"), "restore", "--backup",
               str(tmp_path / "missing"), cwd=cwd).returncode == 2


@windows_only
def test_wrapper_ignores_a_poisoned_path_and_py_launcher(tmp_path):
    fake = tmp_path / "fake bin"
    fake.mkdir()
    hit = tmp_path / "fake-python-was-run.txt"
    for name in ("python.cmd", "python3.cmd", "py.cmd", "python.bat", "py.bat"):
        (fake / name).write_text(f'@echo off\r\necho hit > "{hit}"\r\nexit /b 99\r\n', encoding="ascii")
    env = dict(os.environ)
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    env["PATH"] = os.pathsep.join([str(fake), str(Path(sysroot) / "System32"), str(WINPS.parent)])
    env["PATHEXT"] = ".CMD;.BAT;.EXE"
    r = _ps(WRAPPER, "runtime-info", cwd=tmp_path, env=env)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert Path(d["python_executable"]).resolve() == (ROOT / ".venv-exec/Scripts/python.exe").resolve()
    assert d["runtime_policy"]["operational"] is True and not hit.exists()


@windows_only
def test_wrapper_refuses_without_the_verified_venv_and_never_falls_back(tmp_path):
    repo = tmp_path / "copy with spaces"
    (repo / "scripts").mkdir(parents=True)
    (repo / "config").mkdir()
    shutil.copy(WRAPPER, repo / "scripts" / "aseke-exec.ps1")
    shutil.copy(ROOT / "config/runtime-lock.json", repo / "config" / "runtime-lock.json")
    (repo / ".venv" / "Scripts").mkdir(parents=True)  # an old-style venv must NOT be used
    shutil.copy(OLD_PY if OLD_PY.exists() else sys.executable, repo / ".venv" / "Scripts" / "python.exe")
    r = _ps(repo / "scripts" / "aseke-exec.ps1", "--data-dir", str(tmp_path / "rt"), "init-runtime", cwd=tmp_path)
    assert r.returncode == 4 and "refused" in r.stderr, (r.stdout, r.stderr)
    assert not (tmp_path / "rt").exists()
    # venv present but unverified (no marker written by setup) -> still refused
    (repo / ".venv-exec" / "Scripts").mkdir(parents=True)
    shutil.copy(sys.executable, repo / ".venv-exec" / "Scripts" / "python.exe")
    r = _ps(repo / "scripts" / "aseke-exec.ps1", "runtime-info", cwd=tmp_path)
    assert r.returncode == 4 and "unverified" in r.stderr


@windows_only
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 not installed")
def test_setup_check_survives_psmodulepath_inherited_from_pwsh(tmp_path):
    # Windows PowerShell 5.1 started from pwsh inherits pwsh's PSModulePath; autoloading then picks
    # the 7.x Utility/Archive modules and Get-FileHash is "not recognized". setup.ps1 must not care.
    pwsh_path = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-Command", "$env:PSModulePath"],
                               capture_output=True, text=True, timeout=120).stdout.strip()
    assert pwsh_path
    env = dict(os.environ, PSModulePath=pwsh_path)
    r = _ps(ROOT / "scripts" / "setup.ps1", "-Check", cwd=tmp_path, env=env)  # -Check: read-only
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert "not recognized" not in r.stderr


# ------------------------------------------------------------------ setup: interrupted + repeated
@windows_only
@pytest.mark.skipif(not ZIP.exists() or not WHEELS.exists(), reason="verified local artifacts not present")
def test_interrupted_then_repeated_setup_preserves_old_env_and_data(tmp_path):
    repo = tmp_path / "repo copy with spaces"
    for d in ("src", "config", "scripts", "requirements"):
        shutil.copytree(ROOT / d, repo / d, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    for f in ("pyproject.toml", "README.md"):
        shutil.copy(ROOT / f, repo / f)
    old = repo / ".venv"
    (old / "Scripts").mkdir(parents=True)
    (old / "pyvenv.cfg").write_text("home = C:\\elsewhere\nversion = 3.12.10\n", encoding="utf-8")
    (repo / "var").mkdir()
    (repo / "var" / "journal.db").write_bytes(b"user data - must survive setup")
    (repo / "var" / "journal.db-wal").write_bytes(b"sidecar")
    keep = {p: _sha(p) for p in (old / "pyvenv.cfg", repo / "var" / "journal.db", repo / "var" / "journal.db-wal")}
    setup = repo / "scripts" / "setup.ps1"
    args = ("-ArchivePath", str(ZIP), "-WheelDir", str(WHEELS))

    r1 = _ps(setup, *args, "-TestFailAfter", "after-venv", cwd=tmp_path, timeout=900)
    assert r1.returncode != 0 and "simulated interruption" in (r1.stdout + r1.stderr)
    assert (repo / LOCK["paths"]["interpreter_dir"] / "aseke-provenance.json").exists(), "atomic interpreter install"
    assert not (repo / ".venv-exec" / "aseke-venv.json").exists(), "no marker after an interrupted setup"
    w = _ps(repo / "scripts" / "aseke-exec.ps1", "runtime-info", cwd=tmp_path)
    assert w.returncode == 4, "wrapper refuses a half-built environment"
    assert not list((repo / ".exec-runtime").glob("staging-*")), "no staging leftovers"

    r2 = _ps(setup, *args, cwd=tmp_path, timeout=900)
    assert r2.returncode == 0, r2.stdout[-3000:] + r2.stderr[-3000:]
    marker = repo / ".venv-exec" / "aseke-venv.json"
    m1 = _sha(marker)
    info = _ps(repo / "scripts" / "aseke-exec.ps1", "runtime-info", cwd=tmp_path)
    d = json.loads(info.stdout)
    assert info.returncode == 0 and d["runtime_policy"]["operational"] is True
    assert Path(d["python_executable"]).resolve() == (repo / ".venv-exec/Scripts/python.exe").resolve()

    t0 = time.monotonic()
    r3 = _ps(setup, *args, cwd=tmp_path, timeout=300)
    assert r3.returncode == 0 and "nothing changed" in r3.stdout, r3.stdout
    assert time.monotonic() - t0 < 60 and _sha(marker) == m1, "second run does not reinstall"
    assert {p: _sha(p) for p in keep} == keep, "old .venv and existing runtime data untouched"
    assert _ps(setup, "-Check", cwd=tmp_path).returncode == 0


# ------------------------------------------------------------------ storage compatibility
def test_storage_compat_build_verify_on_approved_runtime(tmp_path):
    """Rich v2 state (partial TP, deadline, INTENT order, tombstones, strict held event) survives a
    quiesced backup -> restore into a NEW root -> reconcile, with integrity and semantics intact."""
    tool = ROOT / "scripts" / "storage_compat.py"
    fx, out = tmp_path / "fixtures", tmp_path / "verified"
    b = subprocess.run([sys.executable, "-I", str(tool), "build", "--python", sys.executable, "--src", str(ROOT),
                        "--out", str(fx)], capture_output=True, text=True, timeout=600)
    assert b.returncode == 0, b.stdout + b.stderr
    v = subprocess.run([sys.executable, "-I", str(tool), "verify", "--fixtures", str(fx), "--out", str(out)],
                       capture_output=True, text=True, timeout=600)
    rep = json.loads((out / "compat-report.json").read_text(encoding="utf-8"))
    assert v.returncode == 0 and rep["result"] == "PASS", rep["problems"]
    a = rep["checks"]["A"]
    assert a["intent_orders_after"] and "INTENT" not in a["intent_orders_after"].values()
    assert all(r["state"] == "REJECTED" for r in a["ghost_open_after_restore"]["executions"])
    assert rep["checks"]["B"]["held_event_after_restore_with_key"], "held signed event ingested once key present"
    # the test-only key never appears in the evidence report or tool output
    key = (fx / "keys" / "sim-demo-k1.key").read_text(encoding="utf-8").strip()
    assert key not in json.dumps(rep) and key not in b.stdout + v.stdout


# ------------------------------------------------------------------ read-only after an unclean writer exit
_WRITER = r"""
import sqlite3, sys
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute("pragma journal_mode=wal"); c.execute("pragma synchronous=full")
c.execute("create table if not exists t(x, pad)")
i = 0
while True:
    c.execute("begin immediate"); c.execute("insert into t values(?, randomblob(3000))", (i,)); c.execute("commit"); i += 1
"""


def test_read_only_open_after_killed_wal_writer_is_retried_bounded(tmp_path):
    """SQLite 3.53.4 on Windows can fail the first read of a NEW mode=ro connection with
    SQLITE_IOERR_TRUNCATE right after a WAL writer was killed. The product opener retries that
    one code (bounded); every attempt here must end readable with the committed rows."""
    w = tmp_path / "writer.py"
    w.write_text(_WRITER, encoding="utf-8")
    raw_failures = 0
    for n in range(12):
        db = tmp_path / f"j{n}.db"
        p = subprocess.Popen([sys.executable, "-I", str(w), str(db)])
        time.sleep(0.2 + 0.03 * (n % 5))
        p.kill()  # only the writer this test started
        p.wait(timeout=30)
        try:  # raw first attempt (informational)
            c = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
            c.execute("select count(*) from t").fetchone()
            c.close()
        except sqlite3.OperationalError as e:
            assert getattr(e, "sqlite_errorname", "") == "SQLITE_IOERR_TRUNCATE", e
            raw_failures += 1
        c = connect_readonly(db)
        assert c.execute("select count(*) from t").fetchone()[0] > 0
        c.close()
    db = tmp_path / "jx.db"
    p = subprocess.Popen([sys.executable, "-I", str(w), str(db)])
    time.sleep(0.3)
    p.kill()
    p.wait(timeout=30)
    with pytest.raises(sqlite3.OperationalError):  # a writable statement is still impossible
        connect_readonly(db).execute("insert into t values (1, 1)")
    print(f"raw first-read failures: {raw_failures}/12")


def test_connect_readonly_does_not_retry_other_errors(tmp_path):
    bad = tmp_path / "not-a-db.db"
    bad.write_bytes(b"this is not sqlite" * 100)
    t0 = time.monotonic()
    with pytest.raises(sqlite3.DatabaseError):
        connect_readonly(bad)
    assert time.monotonic() - t0 < 1.0, "no retry loop for non-transient errors"
    with pytest.raises(FileNotFoundError):
        Journal(tmp_path / "missing.db", readonly=True)
