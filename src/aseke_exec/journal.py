"""Executor-owned durable journal (SQLite).

Durability: WAL + synchronous=FULL, explicit BEGIN IMMEDIATE transactions,
uniqueness constraints for event identity, per-account trade executions and
client order IDs. Keep the file on a LOCAL disk (not a network share).
A single worker process holds ``worker.lock`` (see lock.py).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .clock import iso, parse_utc
from .num import D, s

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  content_hash TEXT NOT NULL,
  trade_id TEXT NOT NULL, event_type TEXT NOT NULL, revision INTEGER NOT NULL,
  producer TEXT NOT NULL, environment TEXT NOT NULL,
  created_at TEXT NOT NULL, source_data_time TEXT, received_at TEXT NOT NULL,
  source_file TEXT, canonical_json TEXT NOT NULL,
  process_status TEXT NOT NULL,            -- PENDING | DEFERRED | APPLIED | REJECTED | STALE
  process_code TEXT, process_message TEXT, defer_until TEXT, processed_at TEXT);
CREATE INDEX IF NOT EXISTS ix_events_status ON events(process_status, seq);
CREATE TABLE IF NOT EXISTS ingest_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, file_name TEXT NOT NULL,
  file_hash TEXT, event_id TEXT, outcome TEXT NOT NULL,   -- JOURNALED|DUPLICATE|CONFLICT|INVALID
  code TEXT, message TEXT, raw_text TEXT);
CREATE TABLE IF NOT EXISTS trades (
  trade_id TEXT PRIMARY KEY, strategy_id TEXT, strategy_version TEXT, instrument_key TEXT,
  direction TEXT, status TEXT NOT NULL,     -- ACTIVE | TOMBSTONED | CLOSE_REQUESTED | CANCEL_REQUESTED
  open_event_id TEXT, last_revision INTEGER NOT NULL DEFAULT 0, tombstone_reason TEXT,
  routing TEXT, config_hash TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_id TEXT NOT NULL, account_id TEXT NOT NULL, venue TEXT NOT NULL, native_symbol TEXT,
  instrument_key TEXT NOT NULL, strategy_id TEXT NOT NULL, direction TEXT NOT NULL,
  leverage INTEGER NOT NULL, state TEXT NOT NULL, entry_outcome TEXT,
  reason_code TEXT, reason_message TEXT,
  policy_snapshot TEXT NOT NULL, instrument_snapshot TEXT, plan TEXT,
  exit_spec TEXT, exit_plan TEXT, holding TEXT, deadline_at TEXT, source_risk TEXT,
  requested_margin TEXT, target_notional TEXT, sizing_price TEXT, limit_price TEXT,
  planned_qty TEXT, planned_qty_btc TEXT, planned_notional TEXT, planned_margin TEXT,
  fee_reservation TEXT, reserved_amount TEXT NOT NULL DEFAULT '0',
  filled_qty TEXT NOT NULL DEFAULT '0', filled_qty_btc TEXT, avg_fill TEXT,
  filled_notional TEXT, actual_margin TEXT, unused_allocation TEXT,
  entry_fees TEXT NOT NULL DEFAULT '0', exit_fees TEXT NOT NULL DEFAULT '0',
  realized_pnl TEXT, net_pnl TEXT, risk_at_stop TEXT, pnl_at_stop TEXT, risk_pct_of_margin TEXT,
  protected_qty TEXT NOT NULL DEFAULT '0',
  first_fill_at TEXT, closed_at TEXT, close_reason TEXT, close_requested TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (trade_id, account_id));
CREATE INDEX IF NOT EXISTS ix_exec_state ON executions(state);
CREATE INDEX IF NOT EXISTS ix_exec_acct ON executions(account_id, instrument_key);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  execution_id INTEGER NOT NULL REFERENCES executions(id),
  account_id TEXT NOT NULL, native_symbol TEXT NOT NULL, client_order_id TEXT NOT NULL,
  leg TEXT NOT NULL, leg_index INTEGER NOT NULL, plan_version INTEGER NOT NULL,
  attempt INTEGER NOT NULL, kind TEXT NOT NULL, side TEXT NOT NULL, qty TEXT NOT NULL,
  limit_price TEXT, trigger_price TEXT, trigger_reference TEXT, reduce_only INTEGER NOT NULL,
  status TEXT NOT NULL,   -- INTENT|UNKNOWN|NOT_ACCEPTED|OPEN|FILLED|CANCELLED|EXPIRED|REJECTED
  venue_order_id TEXT, filled_qty TEXT NOT NULL DEFAULT '0', avg_price TEXT,
  reject_reason TEXT, not_found_checks INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (account_id, client_order_id),
  UNIQUE (execution_id, leg, leg_index, plan_version, attempt));
CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL REFERENCES orders(id),
  ts TEXT NOT NULL, op TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, venue_fill_id TEXT NOT NULL,
  order_id INTEGER NOT NULL REFERENCES orders(id), execution_id INTEGER NOT NULL,
  leg TEXT NOT NULL, qty TEXT NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL,
  fee_currency TEXT NOT NULL, ts TEXT NOT NULL,
  UNIQUE (account_id, venue_fill_id));
CREATE TABLE IF NOT EXISTS execution_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id INTEGER NOT NULL, ts TEXT NOT NULL,
  kind TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS accounts (
  account_id TEXT PRIMARY KEY, strategy_owner TEXT NOT NULL,
  entries_blocked INTEGER NOT NULL DEFAULT 0, block_reason TEXT,
  operator_disabled INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS controls (key TEXT PRIMARY KEY, value TEXT NOT NULL,
  reason TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, execution_id INTEGER,
  kind TEXT NOT NULL, status TEXT NOT NULL, message TEXT, detail TEXT,
  created_at TEXT NOT NULL, resolved_at TEXT, resolution TEXT);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL,
  code TEXT NOT NULL, account_id TEXT, trade_id TEXT, message TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS reconciliations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, account_id TEXT NOT NULL,
  kind TEXT NOT NULL, result TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS config_snapshots (
  hash TEXT PRIMARY KEY, loaded_at TEXT NOT NULL, snapshot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS balance_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, account_id TEXT NOT NULL,
  currency TEXT, wallet TEXT, available TEXT);
"""

_MIGRATION_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "executions": [
        ("stop_coverage", "TEXT"),            # NONE | UNVERIFIED | VERIFIED
        ("tp_coverage", "TEXT"),              # NONE | DEGRADED | COMPLETE
        ("unprotected_since", "TEXT"),        # exposure observed without a verified stop
        ("max_unprotected_seconds", "TEXT"),  # longest measured interval (decimal seconds)
        ("tp_fail_count", "INTEGER NOT NULL DEFAULT 0"),
        ("tp_retry_at", "TEXT"),
        ("stop_fix_retry_at", "TEXT"),
    ],
    "events": [
        ("auth_mode", "TEXT"),                # HMAC | UNSIGNED_COMPAT
        ("auth_key_id", "TEXT"),
        ("envelope_issued_at", "TEXT"),
        ("envelope_hash", "TEXT"),
    ],
}
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS operator_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
  action TEXT NOT NULL, target TEXT, outcome TEXT NOT NULL, detail TEXT);
"""

DECIMAL_COLS = frozenset({
    "requested_margin", "target_notional", "sizing_price", "limit_price", "planned_qty",
    "planned_qty_btc", "planned_notional", "planned_margin", "fee_reservation", "reserved_amount",
    "filled_qty", "filled_qty_btc", "avg_fill", "filled_notional", "actual_margin",
    "unused_allocation", "entry_fees", "exit_fees", "realized_pnl", "net_pnl", "risk_at_stop",
    "pnl_at_stop", "risk_pct_of_margin", "protected_qty", "max_unprotected_seconds", "qty", "trigger_price", "avg_price",
    "price", "fee", "wallet", "available"})
JSON_COLS = frozenset({"policy_snapshot", "instrument_snapshot", "plan", "exit_spec", "exit_plan",
                       "holding", "source_risk", "routing", "detail"})


def _enc(k: str, v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return s(v)
    if isinstance(v, datetime):
        return iso(v)
    if k in JSON_COLS and not isinstance(v, str):
        return json.dumps(v, sort_keys=True, default=lambda o: s(o) if isinstance(o, Decimal) else str(o))
    if isinstance(v, bool):
        return int(v)
    return v


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    out: dict[str, Any] = {}
    for k in r.keys():
        v = r[k]
        if v is not None and k in DECIMAL_COLS:
            v = D(v)
        elif v is not None and k in JSON_COLS:
            v = json.loads(v)
        out[k] = v
    return out


class Journal:
    """Single-writer journal.

    Topology (see docs/runbook.md, SQLite WAL-reset-bug mitigation): exactly one read-write
    connection exists at a time - the worker's (all mutating CLI commands either hold the worker
    lock or queue a control request for the running worker). Status/health readers open
    ``readonly=True`` connections (``mode=ro``) which cannot write or checkpoint.
    """

    def __init__(self, path: str | Path, *, readonly: bool = False, busy_timeout_ms: int = 15000):
        self.path = Path(path)
        self.readonly = readonly
        self._depth = 0
        self._faults: dict[str, int] = {}
        if readonly:
            if not self.path.exists():
                raise FileNotFoundError(f"journal not found: {self.path}")
            self.conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True,
                                        isolation_level=None, timeout=busy_timeout_ms / 1000)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None,
                                    timeout=busy_timeout_ms / 1000)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Idempotent, additive migrations (existing data is preserved)."""
        for table, cols in _MIGRATION_COLUMNS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.executescript(_SCHEMA_V2)
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
                          (str(SCHEMA_VERSION),))

    def schema_version(self) -> int:
        r = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(r[0]) if r else 0

    def close(self) -> None:
        self.conn.close()

    # ---- test-storage fault injection (never armed in production code paths)
    def inject_failure(self, where: str, count: int = 1) -> None:
        """Arm ``count`` simulated failures at ``begin`` or ``commit`` of the next transactions."""
        assert where in ("begin", "commit")
        self._faults[where] = self._faults.get(where, 0) + count

    def _take_fault(self, where: str) -> bool:
        n = self._faults.get(where, 0)
        if n > 0:
            self._faults[where] = n - 1
            return True
        return False

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Re-entrant BEGIN IMMEDIATE transaction. A failed COMMIT is rolled back so the
        connection never stays wedged inside a transaction."""
        if self.readonly:
            raise sqlite3.OperationalError("journal opened read-only")
        if self._depth:
            self._depth += 1
            try:
                yield self.conn
            finally:
                self._depth -= 1
            return
        if self._take_fault("begin"):
            raise sqlite3.OperationalError("database is locked (injected test fault)")
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self.conn
        except BaseException:
            self._depth = 0
            self.conn.execute("ROLLBACK")
            raise
        self._depth = 0
        try:
            if self._take_fault("commit"):
                raise sqlite3.OperationalError("disk I/O error (injected test fault at COMMIT)")
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    # ----------------------------------------------------------- generic
    def q(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [_row(r) for r in self.conn.execute(sql, args).fetchall()]  # type: ignore[misc]

    def q1(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        return _row(self.conn.execute(sql, args).fetchone())

    def insert(self, table: str, **fields: Any) -> int:
        cols = ",".join(fields)
        ph = ",".join("?" for _ in fields)
        with self.tx() as c:
            cur = c.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})",
                            tuple(_enc(k, v) for k, v in fields.items()))
            return int(cur.lastrowid)  # type: ignore[arg-type]

    def update(self, table: str, key: str, key_value: Any, **fields: Any) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE {table} SET {sets} WHERE {key}=?",
                      tuple(_enc(k, v) for k, v in fields.items()) + (key_value,))

    # ----------------------------------------------------------- events
    def event_by_id(self, event_id: str) -> dict[str, Any] | None:
        return self.q1("SELECT * FROM events WHERE event_id=?", (event_id,))

    def pending_events(self) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM events WHERE process_status IN ('PENDING','DEFERRED') ORDER BY seq")

    # ----------------------------------------------------------- trades/executions
    def trade(self, trade_id: str) -> dict[str, Any] | None:
        return self.q1("SELECT * FROM trades WHERE trade_id=?", (trade_id,))

    def execution(self, exec_id: int) -> dict[str, Any]:
        r = self.q1("SELECT * FROM executions WHERE id=?", (exec_id,))
        assert r is not None
        return r

    def executions_for_trade(self, trade_id: str) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM executions WHERE trade_id=? ORDER BY id", (trade_id,))

    def active_executions(self, terminal: frozenset[str]) -> list[dict[str, Any]]:
        ph = ",".join("?" for _ in terminal)
        return self.q(f"SELECT * FROM executions WHERE state NOT IN ({ph}) ORDER BY id",
                      tuple(sorted(terminal)))

    def update_execution(self, exec_id: int, now: datetime, **fields: Any) -> None:
        self.update("executions", "id", exec_id, updated_at=now, **fields)

    def log_execution(self, exec_id: int, now: datetime, kind: str, **detail: Any) -> None:
        self.insert("execution_log", execution_id=exec_id, ts=now, kind=kind, detail=detail)

    # ----------------------------------------------------------- orders/fills
    def orders_for(self, exec_id: int) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM orders WHERE execution_id=? ORDER BY id", (exec_id,))

    def order(self, order_id: int) -> dict[str, Any]:
        r = self.q1("SELECT * FROM orders WHERE id=?", (order_id,))
        assert r is not None
        return r

    def next_attempt(self, exec_id: int, leg: str, leg_index: int, plan_version: int) -> int:
        r = self.conn.execute("SELECT COALESCE(MAX(attempt), -1) + 1 FROM orders WHERE execution_id=? "
                              "AND leg=? AND leg_index=? AND plan_version=?",
                              (exec_id, leg, leg_index, plan_version)).fetchone()
        return int(r[0])

    def known_client_ids(self, account_id: str) -> set[str]:
        return {r[0] for r in self.conn.execute(
            "SELECT client_order_id FROM orders WHERE account_id=?", (account_id,))}

    def add_fill(self, **f: Any) -> bool:
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO fills (account_id, venue_fill_id, order_id, execution_id, leg,"
                " qty, price, fee, fee_currency, ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                tuple(_enc(k, f[k]) for k in ("account_id", "venue_fill_id", "order_id",
                                              "execution_id", "leg", "qty", "price", "fee",
                                              "fee_currency", "ts")))
            return cur.rowcount == 1

    def fills_for(self, exec_id: int, legs: tuple[str, ...]) -> list[dict[str, Any]]:
        ph = ",".join("?" for _ in legs)
        return self.q(f"SELECT * FROM fills WHERE execution_id=? AND leg IN ({ph}) ORDER BY ts, id",
                      (exec_id, *legs))

    # ----------------------------------------------------------- accounts/controls
    def account(self, account_id: str) -> dict[str, Any] | None:
        return self.q1("SELECT * FROM accounts WHERE account_id=?", (account_id,))

    def control(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM controls WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_control(self, key: str, value: str, reason: str | None, now: datetime) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO controls VALUES (?,?,?,?)", (key, value, reason, iso(now)))

    def open_incidents(self, account_id: str | None = None) -> list[dict[str, Any]]:
        if account_id is None:
            return self.q("SELECT * FROM incidents WHERE status='OPEN' ORDER BY id")
        return self.q("SELECT * FROM incidents WHERE status='OPEN' AND account_id=? ORDER BY id",
                      (account_id,))


def parse_ts(v: str | None) -> datetime | None:
    return parse_utc(v) if v else None
