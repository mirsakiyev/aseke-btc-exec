"""Runtime policy: which Python/SQLite build may run the executor (docs/runbook.md).

Three SEPARATE facts are reported, never conflated:

1. ``wal_reset_fix`` - does the SQLite library ACTUALLY LOADED by this process contain the
   upstream WAL-reset-bug fix? Classified from the parsed numeric version per
   https://sqlite.org/wal.html: affected 3.7.0 .. 3.51.2; fixed in 3.51.3 and later; backports
   3.44.6 (3.44 line) and 3.50.7 (3.50 line).
2. ``approved_build`` - is this exactly the pinned project build (config/runtime-lock.json):
   Python version/implementation, 64-bit, non-free-threaded, loaded SQLite version AND source id,
   and the interpreter's base prefix inside this checkout's project-private directory?
3. ``extended_soak`` - evidence status from the lock file (informational; "PENDING" until an
   operator-run extended soak has been reviewed). It is never inferred from a version check.

``operational`` (may open execution databases / process signals and controls) requires 1 and 2
and no release-policy exclusion. A version check is not a universal security or reliability
guarantee. There is deliberately NO environment-variable bypass.
"""

from __future__ import annotations

import functools
import json
import re
import sqlite3
import struct
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = REPO_ROOT / "config" / "runtime-lock.json"

#: Release-policy exclusions (exact versions), even if the fix is nominally present.
EXCLUDED_RELEASES: dict[tuple[int, int, int], str] = {
    (3, 52, 0): "withdrawn upstream on 2026-03-13 (https://sqlite.org/news.html)",
    (3, 54, 0): "superseded by patch 3.54.1 (heap buffer overflow on some 32-bit builds)",
}

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?$")


def parse_sqlite_version(version: str | None) -> tuple[int, int, int] | None:
    """Strict numeric parse ("3.53.4" or "3.53.4.0"); anything else -> None (UNKNOWN)."""
    if not isinstance(version, str):
        return None
    m = _VERSION_RE.match(version.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def classify_wal_reset(version: str | None) -> str:
    """FIXED | FIXED_BACKPORT | AFFECTED | NOT_APPLICABLE (pre-WAL) | UNKNOWN."""
    v = parse_sqlite_version(version)
    if v is None:
        return "UNKNOWN"
    if v >= (3, 51, 3):
        return "FIXED"
    if (3, 50, 7) <= v < (3, 51, 0) or (3, 44, 6) <= v < (3, 45, 0):
        return "FIXED_BACKPORT"
    if (3, 7, 0) <= v <= (3, 51, 2):
        return "AFFECTED"
    return "NOT_APPLICABLE"  # < 3.7.0: no WAL at all; never approved either


def release_exclusion(version: str | None) -> str | None:
    v = parse_sqlite_version(version)
    return EXCLUDED_RELEASES.get(v) if v else None


def probe() -> dict[str, Any]:
    """Facts about THIS process. In-memory SQLite only - never opens a runtime database."""
    c = sqlite3.connect(":memory:")
    try:
        lib_version, source_id = c.execute("SELECT sqlite_version(), sqlite_source_id()").fetchone()
    finally:
        c.close()
    try:
        import _sqlite3  # noqa: PLC0415
        ext = getattr(_sqlite3, "__file__", None)
    except ImportError:  # pragma: no cover
        ext = None
    return {
        "executable": sys.executable,
        "base_executable": getattr(sys, "_base_executable", None),
        "prefix": sys.prefix, "base_prefix": sys.base_prefix,
        "python_version": "%d.%d.%d" % sys.version_info[:3],
        "python_full_version": sys.version,
        "implementation": sys.implementation.name,
        "pointer_bits": struct.calcsize("P") * 8,
        "free_threaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
        "sqlite_module_version": sqlite3.sqlite_version,
        "sqlite_version": lib_version, "sqlite_source_id": source_id,
        "sqlite3_module": getattr(sqlite3, "__file__", None), "_sqlite3_module": ext,
    }


def load_lock(path: Path = LOCK_PATH) -> dict[str, Any] | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("format") == "aseke.exec.runtime-lock/1" else None


@dataclass
class PolicyStatus:
    wal_reset_fix: str
    release_exclusion: str | None
    approved_build: bool
    approved_build_id: str | None
    extended_soak: str
    reasons: list[str] = field(default_factory=list)
    probe: dict[str, Any] = field(default_factory=dict)

    @property
    def operational(self) -> bool:
        return (self.wal_reset_fix in ("FIXED", "FIXED_BACKPORT") and self.release_exclusion is None
                and self.approved_build)

    def as_dict(self) -> dict[str, Any]:
        return {"operational": self.operational, "wal_reset_fix": self.wal_reset_fix,
                "release_exclusion": self.release_exclusion, "approved_build": self.approved_build,
                "approved_build_id": self.approved_build_id, "extended_soak": self.extended_soak,
                "reasons": list(self.reasons),
                "lock_file": str(LOCK_PATH),
                "reference": "https://sqlite.org/wal.html (WAL-reset bug); https://sqlite.org/news.html"}


def evaluate(p: dict[str, Any], lock: dict[str, Any] | None, repo_root: Path = REPO_ROOT) -> PolicyStatus:
    """Pure decision function (unit-tested with synthetic probes)."""
    reasons: list[str] = []
    wal = classify_wal_reset(p.get("sqlite_version"))
    excl = release_exclusion(p.get("sqlite_version"))
    if wal not in ("FIXED", "FIXED_BACKPORT"):
        reasons.append(f"loaded SQLite {p.get('sqlite_version')!r} WAL-reset fix status: {wal}")
    if excl:
        reasons.append(f"SQLite {p.get('sqlite_version')} excluded by release policy: {excl}")
    module_consistent = p.get("sqlite_module_version") == p.get("sqlite_version")
    if not module_consistent:
        reasons.append("sqlite3 module version differs from the library reported by SQL")
    if lock is None:
        reasons.append(f"runtime lock missing or invalid: {LOCK_PATH}")
        return PolicyStatus(wal, excl, False, None, "UNKNOWN", reasons, p)
    py, sq, paths = lock.get("python", {}), lock.get("sqlite", {}), lock.get("paths", {})
    checks = [
        (p.get("implementation") == py.get("implementation"), "implementation"),
        (p.get("python_version") == py.get("version"), "Python version"),
        (p.get("pointer_bits") == 64, "64-bit interpreter"),
        (p.get("free_threaded") is bool(py.get("free_threaded")), "free-threading build flag"),
        (p.get("sqlite_version") == sq.get("version"), "SQLite version"),
        (p.get("sqlite_source_id") == sq.get("source_id"), "SQLite source id"),
    ]
    for ok, what in checks:
        if not ok:
            reasons.append(f"{what} does not match the approved build {lock.get('approved_build_id')}")
    want = paths.get("interpreter_dir")
    try:
        same_dir = bool(want) and (Path(p.get("base_prefix", "")).resolve()
                                   == (repo_root / want).resolve())
    except OSError:
        same_dir = False
    if not same_dir:
        reasons.append(f"interpreter base prefix {p.get('base_prefix')!r} is not the project-private "
                       f"runtime {want!r} (run scripts\\setup.ps1; use scripts\\aseke-exec.ps1)")
    approved = all(ok for ok, _ in checks) and same_dir and module_consistent
    soak = str(lock.get("evidence", {}).get("extended_soak", "UNKNOWN"))
    return PolicyStatus(wal, excl, approved, lock.get("approved_build_id"), soak, reasons, p)


@functools.lru_cache(maxsize=1)
def current() -> PolicyStatus:
    return evaluate(probe(), load_lock())


class RuntimeNotApproved(RuntimeError):
    """Raised before any execution database is opened on an unapproved runtime."""


def require_operational(purpose: str) -> None:
    st = current()
    if not st.operational:
        raise RuntimeNotApproved(
            f"{purpose} refused: this Python/SQLite runtime is not approved for operation "
            f"(SQLite {st.probe.get('sqlite_version')}, {st.wal_reset_fix}; "
            f"python {st.probe.get('executable')}). " + "; ".join(st.reasons))
