"""Operator safety tooling: runtime fingerprint, health, fresh-runtime init, backup/restore.

Backup protocol (docs/runbook.md): the worker lock AND the simulator-harness lock are taken
(quiescing every writer), each SQLite database is copied with the SQLite Online Backup API
(``sqlite3.Connection.backup``) - never a raw copy of only the main ``.db`` file - and a
manifest with one generation id, config hash and file hashes is written LAST. Restore goes to
a NEW runtime root only; the first cycle there reconciles before any entry.
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.metadata as md
import json
import os
import platform
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .clock import SystemClock, iso, parse_utc
from .config import Config
from .engine import TERMINAL
from .journal import SCHEMA_VERSION, Journal
from .lock import ProcessLock

PKG_DIR = Path(__file__).resolve().parent
RUNTIME_DIRS = ("inbox", "archive", "quarantine", "control")


# ------------------------------------------------------------------ runtime fingerprint
def wal_reset_status(version: str) -> dict[str, Any]:
    """Classify the embedded SQLite against https://sqlite.org/wal.html section 'WAL-reset bug'
    (affected 3.7.0 .. 3.51.2; fixed 3.51.3+; backports 3.44.6 and 3.50.7)."""
    v = tuple(int(x) for x in version.split(".")[:3])
    if v >= (3, 51, 3):
        st = "FIXED"
    elif (3, 50, 7) <= v < (3, 51, 0) or (3, 44, 6) <= v < (3, 45, 0):
        st = "FIXED_BACKPORT"
    elif (3, 7, 0) <= v <= (3, 51, 2):
        st = "AFFECTED"
    else:
        st = "NOT_APPLICABLE"
    return {"sqlite_version": version, "wal_reset_bug": st,
            "reference": "https://sqlite.org/wal.html (WAL-reset bug section)"}


def source_fingerprint() -> str:
    h = hashlib.sha256()
    for f in sorted(PKG_DIR.glob("*.py")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def git_revision(repo: Path) -> str | None:
    """Read-only: resolve .git/HEAD without invoking git."""
    head = repo / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref:"):
            p = repo / ".git" / ref.split(" ", 1)[1]
            if p.exists():
                return p.read_text(encoding="utf-8").strip()
            packed = repo / ".git" / "packed-refs"
            for line in packed.read_text(encoding="utf-8").splitlines() if packed.exists() else []:
                if line.endswith(ref.split(" ", 1)[1]):
                    return line.split(" ")[0]
            return None
        return ref
    except OSError:
        return None


def runtime_info(cfg: Config | None = None) -> dict[str, Any]:
    c = sqlite3.connect(":memory:")
    source_id = c.execute("select sqlite_source_id()").fetchone()[0]
    c.close()
    deps = {}
    for name in ("aseke-btc-exec", "pytest", "jsonschema", "pyflakes"):
        try:
            deps[name] = md.version(name)
        except md.PackageNotFoundError:
            deps[name] = None
    repo = PKG_DIR.parents[1]
    info: dict[str, Any] = {
        "python_executable": sys.executable, "python_version": sys.version,
        "platform": platform.platform(), "sqlite_source_id": source_id,
        **wal_reset_status(sqlite3.sqlite_version), "dependencies": deps,
        "source_fingerprint_sha256": source_fingerprint(), "git_head": git_revision(repo),
    }
    if cfg is not None:
        info["config"] = {"path": str(cfg.config_path) if cfg.config_path else None,
                          "source_hash": cfg.source_hash, "data_dir": str(cfg.data_dir),
                          "auth": cfg.auth.fingerprint()}  # redacted: no key material
    return info


class _PMC(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS; defined ONCE (per-call classes leak)
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


_MEMFN: Any = None


def process_memory_bytes() -> int | None:
    """Working set of this process (Windows K32GetProcessMemoryInfo); None elsewhere."""
    global _MEMFN
    if os.name != "nt":
        return None
    try:
        k32 = ctypes.windll.kernel32
        if _MEMFN is None:
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            fn = k32.K32GetProcessMemoryInfo
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_ulong]
            fn.restype = ctypes.c_int
            _MEMFN = fn
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        if _MEMFN(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return int(pmc.WorkingSetSize)
    except (AttributeError, OSError):
        return None
    return None


# ------------------------------------------------------------------ health
def pid_alive(pid: int | None) -> bool | None:
    """Non-intrusive liveness hint (never signals the process). On Windows os.kill(pid, 0)
    would TERMINATE the process, so OpenProcess/GetExitCodeProcess is used instead."""
    if not pid:
        return None
    if os.name == "nt":
        try:
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = ctypes.c_void_p
            h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
            k32.CloseHandle(ctypes.c_void_p(h))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except (AttributeError, OSError):
            return None
    try:
        os.kill(int(pid), 0)  # POSIX: signal 0 only checks existence
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _age(now: datetime, ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return round((now - parse_utc(ts)).total_seconds(), 3)
    except ValueError:
        return None


def health_report(cfg: Config) -> dict[str, Any]:
    """Machine-readable health. Distinguishes a RUNNING process (fresh heartbeat) from a
    HEALTHY trading lifecycle (no incidents, verified stops, complete TPs, fresh quotes...).
    Opens the journal read-only (mode=ro), so it never writes or checkpoints."""
    now = SystemClock().now()
    reasons: list[str] = []
    hb: dict[str, Any] | None = None
    if cfg.heartbeat_path.exists():
        try:
            hb = json.loads(cfg.heartbeat_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            hb = None
    hb_age = _age(now, hb.get("wall_written_at")) if hb else None
    stale_after = max(3 * float(hb.get("poll_interval_seconds", cfg.poll_interval_seconds)) if hb else 0, 15.0)
    kind = (hb or {}).get("kind", "worker")
    alive = pid_alive((hb or {}).get("pid"))
    running = (kind == "worker" and hb_age is not None and hb_age <= stale_after and alive is not False)
    if not running:
        reasons.append("no resident worker: heartbeat " + ("missing" if hb is None else
                       f"from a {kind} run, age {hb_age}s, pid alive={alive}") +
                       " (exits, stops and time exits are only managed while a worker runs)")
    metrics = (hb or {}).get("metrics", {})
    out: dict[str, Any] = {
        "generated_at": iso(now), "data_dir": str(cfg.data_dir),
        "process": {"running": running, "heartbeat_kind": kind, "pid_alive": alive,
                    "heartbeat_age_seconds": hb_age, "stale_after_seconds": stale_after,
                    "pid": (hb or {}).get("pid"), "cycles": (hb or {}).get("cycles"),
                    "last_cycle_completed_at": metrics.get("last_cycle_completed_at"),
                    "last_cycle_ms": metrics.get("last_cycle_ms"), "cycle_errors": metrics.get("cycle_errors"),
                    "last_stage_errors": metrics.get("last_stage_errors"),
                    "held_inbox_files": metrics.get("held_files")},
        "quote_age_seconds": metrics.get("quote_age_seconds", {}),
        "runtime": wal_reset_status(sqlite3.sqlite_version),
    }
    if out["runtime"]["wal_reset_bug"] == "AFFECTED":
        reasons.append(f"embedded SQLite {sqlite3.sqlite_version} is affected by the WAL-reset bug "
                       "(single-writer mitigation active; unattended operation not certified)")
    for aid, age in (metrics.get("quote_age_seconds") or {}).items():
        if age is None or age > cfg.execution.max_quote_age_seconds:
            reasons.append(f"{aid}: quote unavailable/stale ({age})")
    if metrics.get("last_stage_errors"):
        reasons.append(f"last cycle stage errors: {sorted(metrics['last_stage_errors'])}")
    if not cfg.journal_path.exists():
        out["lifecycle"] = {"healthy": False, "reasons": reasons + ["no journal yet"]}
        return out
    j = Journal(cfg.journal_path, readonly=True)
    try:
        if j.schema_version() < SCHEMA_VERSION:
            out["lifecycle"] = {"healthy": False, "reasons": reasons + [
                f"journal schema v{j.schema_version()} < v{SCHEMA_VERSION}: run one worker cycle to migrate"]}
            return out
        active = j.active_executions(TERMINAL)
        execs = []
        for e in active:
            legs = (e["exit_plan"] or {}).get("legs", [])
            unprot = _age(now, e["unprotected_since"])
            execs.append({"id": e["id"], "trade_id": e["trade_id"], "account_id": e["account_id"],
                          "state": e["state"], "stop_coverage": e["stop_coverage"],
                          "tp_coverage": e["tp_coverage"], "unprotected_age_seconds": unprot,
                          "max_unprotected_seconds": str(e["max_unprotected_seconds"])
                          if e["max_unprotected_seconds"] is not None else None,
                          "tp_fail_count": e["tp_fail_count"], "tp_retry_at": e["tp_retry_at"],
                          "legs": [{k: l.get(k) for k in ("index", "price", "qty", "executed_qty",
                                                          "remaining_qty")} for l in legs]})
            if e["state"] in ("OPEN_UNPROTECTED", "OPEN_PROTECTED", "CLOSING"):
                if e["stop_coverage"] != "VERIFIED":
                    reasons.append(f"execution {e['id']}: stop coverage {e['stop_coverage']} "
                                   f"(unprotected for {unprot}s)")
                if e["tp_coverage"] == "DEGRADED":
                    reasons.append(f"execution {e['id']}: take-profit coverage DEGRADED")
            if e["state"] in ("ENTRY_UNKNOWN", "CLOSING"):
                reasons.append(f"execution {e['id']}: {e['state']}")
        unknown = j.q("SELECT id, account_id, leg, client_order_id, status, not_found_checks, created_at "
                      "FROM orders WHERE status IN ('INTENT','UNKNOWN') ORDER BY id")
        for o in unknown:
            o["age_seconds"] = _age(now, o["created_at"])
        if unknown:
            reasons.append(f"{len(unknown)} order(s) with unresolved outcome")
        incidents = j.open_incidents()
        if incidents:
            reasons.append(f"{len(incidents)} unresolved incident(s)")
        pending = j.q("SELECT event_id, event_type, process_status, received_at FROM events WHERE "
                      "process_status IN ('PENDING','DEFERRED') ORDER BY seq")
        for p in pending:
            p["age_seconds"] = _age(now, p["received_at"])
        accounts = []
        for a in j.q("SELECT * FROM accounts ORDER BY account_id"):
            mine = [e for e in active if e["account_id"] == a["account_id"]]
            gate = []
            if a["entries_blocked"]:
                gate.append(f"BLOCKED: {a['block_reason']}")
            if a["operator_disabled"]:
                gate.append("DISABLED by operator")
            for e in mine:
                if e["tp_coverage"] == "DEGRADED":
                    gate.append(f"DEGRADED: execution {e['id']} take-profit coverage")
                if e["stop_coverage"] == "UNVERIFIED":
                    gate.append(f"DEGRADED: execution {e['id']} stop coverage unverified")
            accounts.append({
                "account_id": a["account_id"], "strategy_owner": a["strategy_owner"],
                "entries_blocked": bool(a["entries_blocked"]), "block_reason": a["block_reason"],
                "operator_disabled": bool(a["operator_disabled"]),
                "new_entry_gate": gate or ["open"],
                "planned_margin": str(sum((e["planned_margin"] or Decimal(0) for e in mine), Decimal(0))),
                "actual_margin": str(sum((e["actual_margin"] or Decimal(0) for e in mine), Decimal(0))),
                "reserved_margin": str(sum((e["reserved_amount"] or Decimal(0) for e in mine), Decimal(0))),
                "last_reconcile": (j.q1("SELECT ts, kind, result FROM reconciliations WHERE account_id=? "
                                        "ORDER BY id DESC LIMIT 1", (a["account_id"],)) or {}),
            })
            if a["entries_blocked"]:
                reasons.append(f"{a['account_id']}: entries blocked ({a['block_reason']})")
        out.update({
            "journal_schema_version": j.schema_version(),
            "controls": {r["key"]: r["value"] for r in j.q("SELECT * FROM controls")},
            "accounts": accounts, "active_executions": execs, "unresolved_orders": unknown,
            "open_orders": j.q1("SELECT COUNT(*) AS n FROM orders WHERE status='OPEN'")["n"],  # type: ignore[index]
            "open_incidents": incidents, "pending_events": pending,
        })
    finally:
        j.close()
    out["lifecycle"] = {"healthy": running and not reasons, "reasons": reasons}
    return out


# ------------------------------------------------------------------ runtime roots
def runtime_has_state(data_dir: Path) -> bool:
    return data_dir.exists() and any(data_dir.iterdir())


def init_runtime(cfg: Config) -> dict[str, Any]:
    """Create a FRESH isolated runtime root (preferred over any reset)."""
    if runtime_has_state(cfg.data_dir):
        raise FileExistsError(f"refusing to initialise non-empty runtime root {cfg.data_dir}; "
                              "choose a new --data-dir")
    from .runtime import build_runtime
    with ProcessLock(cfg.lock_path):
        rt = build_runtime(cfg)
        try:
            for d in RUNTIME_DIRS:
                (cfg.data_dir / d).mkdir(parents=True, exist_ok=True)
        finally:
            rt.close()
    return {"data_dir": str(cfg.data_dir), "journal": str(cfg.journal_path), "sim": str(cfg.sim.db_path)}


def journal_has_history(cfg: Config) -> bool:
    if not cfg.journal_path.exists():
        return False
    j = Journal(cfg.journal_path, readonly=True)
    try:
        return bool(j.q1("SELECT COUNT(*) AS n FROM orders")["n"]  # type: ignore[index]
                    or j.q1("SELECT COUNT(*) AS n FROM executions")["n"])  # type: ignore[index]
    finally:
        j.close()


# ------------------------------------------------------------------ backup / restore
def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sqlite_copy(src: Path, dst: Path) -> None:
    """SQLite Online Backup API: consistent snapshot including committed WAL content.
    The source is opened read-only; no sidecar file is touched manually."""
    s = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")  # self-contained artifact (no -wal needed)
    finally:
        d.close()
        s.close()


def _counts(db: Path, tables: tuple[str, ...]) -> dict[str, int]:
    c = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        c.close()


def backup_runtime(cfg: Config, dest: Path) -> dict[str, Any]:
    """Quiesced, coordinated simulator+journal backup to a NEW destination."""
    dest = Path(dest).resolve()
    src_root = cfg.data_dir.resolve()
    if runtime_has_state(dest):
        raise FileExistsError(f"backup destination {dest} exists and is not empty")
    if dest == src_root or src_root in dest.parents or dest in src_root.parents:
        raise ValueError("backup destination must be outside the runtime root")
    for f in (cfg.journal_path, cfg.sim.db_path):
        if not f.exists():
            raise FileNotFoundError(f)
    # Maintenance protocol: both locks are held for the whole copy (no worker, no harness writes).
    with ProcessLock(cfg.lock_path), ProcessLock(cfg.sim_lock_path):
        dest.mkdir(parents=True, exist_ok=True)
        files = {"journal.db": cfg.journal_path, "sim_exchange.db": cfg.sim.db_path}
        for name, src in files.items():
            _sqlite_copy(src, dest / name)
        for d in RUNTIME_DIRS:
            if (cfg.data_dir / d).exists():
                shutil.copytree(cfg.data_dir / d, dest / d)
        manifest = {
            "format": "aseke.exec.backup/1", "generation": uuid.uuid4().hex,
            "created_at": iso(SystemClock().now()), "source_data_dir": str(cfg.data_dir),
            "quiesced": True, "config_hash": cfg.source_hash, "journal_schema_version": SCHEMA_VERSION,
            "sqlite_version": sqlite3.sqlite_version,
            "files": {n: {"sha256": _sha256(dest / n), "bytes": (dest / n).stat().st_size} for n in files},
            "row_counts": {"journal.db": _counts(dest / "journal.db", ("events", "executions", "orders", "fills")),
                           "sim_exchange.db": _counts(dest / "sim_exchange.db", ("orders", "fills", "positions"))},
        }
        tmp = dest / ".manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.replace(tmp, dest / "manifest.json")  # written LAST: its presence marks completeness
    return manifest


def restore_runtime(backup: Path, cfg_dest: Config) -> dict[str, Any]:
    """Restore a verified backup into a NEW, empty runtime root (never over a live runtime)."""
    backup = Path(backup).resolve()
    dest = cfg_dest.data_dir
    man_path = backup / "manifest.json"
    if not man_path.exists():
        raise FileNotFoundError("manifest.json missing: incomplete or foreign backup")
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "aseke.exec.backup/1":
        raise ValueError("unsupported backup format")
    if int(manifest.get("journal_schema_version", 0)) > SCHEMA_VERSION:
        raise ValueError("backup was made by a newer journal schema")
    for name, meta in manifest["files"].items():
        if _sha256(backup / name) != meta["sha256"]:
            raise ValueError(f"{name}: checksum mismatch (corrupt backup)")
    dest = dest.resolve()
    if runtime_has_state(dest):
        raise FileExistsError(f"restore destination {dest} is not empty; restore only into a NEW root")
    for target in (cfg_dest.journal_path, cfg_dest.sim.db_path):
        t = target.resolve()
        if dest not in t.parents or t.exists():
            raise ValueError(f"restore target {t} must be a NEW file inside {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    _sqlite_copy(backup / "journal.db", cfg_dest.journal_path)
    _sqlite_copy(backup / "sim_exchange.db", cfg_dest.sim.db_path)
    for d in RUNTIME_DIRS:
        if (backup / d).exists():
            shutil.copytree(backup / d, dest / d)
        else:
            (dest / d).mkdir(parents=True, exist_ok=True)
    info = {"restored_from": str(backup), "generation": manifest["generation"],
            "restored_at": iso(SystemClock().now()),
            "config_hash_matches": manifest.get("config_hash") == cfg_dest.source_hash}
    (dest / "restored-from.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info
