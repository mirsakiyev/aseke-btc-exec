"""Execution lifecycle engine.

One worker process, sequential processing (which serializes every action for a
given account/instrument). Each per-account execution is isolated with its own
error handling, so one unhealthy account never stops management of another.

Cycle: [startup reconcile once] -> ingest inbox -> apply journaled events ->
manage every active execution (entry reconciliation, protection desired-state,
TP/stop discovery, time exits, closing) -> periodic unowned-exposure scan ->
status snapshot.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from .adapter import (
    AdapterError, AdapterRejected, ExchangeAdapter, OrderKind, OrderReport,
    OrderRequest, Position,
)
from .clock import Clock, iso
from .config import AccountConfig, Config
from .contract import canonical_json, content_hash, load_event_json, parse_event
from .domain import (
    Direction, Environment, Event, EventType, ExitSpec, HoldingRule, InstrumentRules, LevelPolicy,
    MarginMode, PriceRef, Side,
)
from .auth import Authenticator, AuthRejection
from .faults import crashpoint
from .inbox import Inbox
from .journal import Journal, parse_ts
from .num import ZERO, D, s
from .planning import (
    allocate_take_profits, check_geometry, downside_at_stop, entry_limit_price, executable_depth,
    levels_are_tick_aligned, pnl_at_stop, price_within_bound, resolve_levels, size_position, vwap,
)
from .reasons import SKIP_CODES, R, Rejection

log = logging.getLogger("aseke_exec.engine")


class ExecState(StrEnum):
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"
    RESERVED = "RESERVED"
    ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
    ENTRY_UNKNOWN = "ENTRY_UNKNOWN"
    ENTRY_VENUE_REJECTED = "ENTRY_VENUE_REJECTED"
    ENTRY_NOT_FILLED = "ENTRY_NOT_FILLED"
    ENTRY_NOT_ACCEPTED = "ENTRY_NOT_ACCEPTED"
    ENTRY_CANCELLED = "ENTRY_CANCELLED"
    OPEN_UNPROTECTED = "OPEN_UNPROTECTED"
    OPEN_PROTECTED = "OPEN_PROTECTED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


TERMINAL = frozenset({ExecState.REJECTED, ExecState.SKIPPED, ExecState.ENTRY_VENUE_REJECTED,
                      ExecState.ENTRY_NOT_FILLED, ExecState.ENTRY_NOT_ACCEPTED,
                      ExecState.ENTRY_CANCELLED, ExecState.CLOSED})
PRE_FILL = frozenset({ExecState.RESERVED, ExecState.ENTRY_SUBMITTED, ExecState.ENTRY_UNKNOWN})
OPEN_STATES = frozenset({ExecState.OPEN_UNPROTECTED, ExecState.OPEN_PROTECTED})
RESERVING = PRE_FILL
UNRESOLVED_ORDER = ("INTENT", "UNKNOWN")
LIVE_ORDER = ("INTENT", "UNKNOWN", "OPEN")
EXIT_LEGS = ("STOP", "TP", "CLOSE")


def client_order_id(trade_id: str, account_id: str, leg: str, leg_index: int, plan_version: int,
                    attempt: int) -> str:
    """Deterministic client order id derived from the logical order identity."""
    h = hashlib.sha256(f"{trade_id}|{account_id}|{leg}|{leg_index}|{plan_version}|{attempt}"
                       .encode()).hexdigest()[:22]
    return f"AX{leg[0]}{h}"


@dataclass
class Preflight:
    acct: AccountConfig
    rules: InstrumentRules
    symbol: str
    limit_price: Decimal
    sizing_price: Decimal
    expected_fill: Decimal
    sizing: Any
    fee_reservation: Decimal
    reservation: Decimal
    available: Decimal
    exit_spec: ExitSpec


class Engine:
    def __init__(self, config: Config, journal: Journal, adapters: dict[str, ExchangeAdapter],
                 clock: Clock, inbox: Inbox | None = None,
                 alert_sink: Callable[[dict[str, Any]], None] | None = None):
        self.cfg = config
        self.j = journal
        self.adapters = adapters
        self.clock = clock
        self.inbox = inbox
        self.alert_sink = alert_sink
        self.ready: set[str] = set()
        self.started = False
        self.cycles = 0
        self.auth = Authenticator(config.auth, clock)
        self._alert_seen: dict[tuple[Any, ...], tuple[float, int]] = {}
        self._acct_errors: dict[str, int] = {}
        # Operational metrics for heartbeat/health (not trading state).
        self.metrics: dict[str, Any] = {"last_cycle_started_at": None, "last_cycle_completed_at": None,
                                        "last_cycle_ms": None, "cycle_errors": 0,
                                        "last_stage_errors": {}, "last_reconcile_at": {},
                                        "quote_age_seconds": {}, "held_files": 0}
        self._register_accounts(config)

    # ============================================================ configuration
    def _register_accounts(self, cfg: Config) -> None:
        """Validate owner assignments against persisted state, then persist them."""
        now = self.clock.now()
        problems: list[str] = []
        for aid, row in {r["account_id"]: r for r in self.j.q("SELECT * FROM accounts")}.items():
            if aid not in cfg.accounts and self._account_busy(aid):
                problems.append(f"account {aid} removed from config while it still has exposure, "
                                "pending entries or open incidents; disable it instead")
        for aid, acct in cfg.accounts.items():
            row = self.j.account(aid)
            if row and row["strategy_owner"] != acct.strategy_owner and self._account_busy(aid):
                problems.append(f"OWNER_REASSIGNMENT_BLOCKED: account {aid} owner "
                                f"{row['strategy_owner']} -> {acct.strategy_owner} rejected until the "
                                "previous owner has no exposure/pending entries/protective orders/"
                                "unresolved submissions")
        if problems:
            from .config import ConfigError
            raise ConfigError(problems)
        for aid, acct in cfg.accounts.items():
            row = self.j.account(aid)
            if row is None:
                self.j.insert("accounts", account_id=aid, strategy_owner=acct.strategy_owner,
                              updated_at=now)
            elif row["strategy_owner"] != acct.strategy_owner:
                self.j.update("accounts", "account_id", aid, strategy_owner=acct.strategy_owner,
                              updated_at=now)
        self.j.conn.execute("INSERT OR IGNORE INTO config_snapshots VALUES (?,?,?)",
                            (cfg.source_hash, iso(now), json.dumps(
                                {a: c.snapshot() for a, c in cfg.accounts.items()}, sort_keys=True)))

    def _account_busy(self, account_id: str) -> bool:
        ph = ",".join("?" for _ in TERMINAL)
        busy = self.j.conn.execute(
            f"SELECT COUNT(*) FROM executions WHERE account_id=? AND state NOT IN ({ph})",
            (account_id, *sorted(TERMINAL))).fetchone()[0]
        live = self.j.conn.execute(
            "SELECT COUNT(*) FROM orders WHERE account_id=? AND status IN ('INTENT','UNKNOWN','OPEN')",
            (account_id,)).fetchone()[0]
        return bool(busy or live or self.j.open_incidents(account_id))

    def apply_config(self, cfg: Config) -> None:
        """Hot reload. Affects future entries only; existing executions keep their snapshot.
        The authentication profile (mode / audience) can only change with a worker restart."""
        if (cfg.auth.mode, cfg.auth.audience) != (self.cfg.auth.mode, self.cfg.auth.audience):
            from .config import ConfigError
            self.alert("CRITICAL", "AUTH_PROFILE_CHANGE_REFUSED",
                       f"hot reload tried to change auth mode/audience {self.cfg.auth.mode}->{cfg.auth.mode}; "
                       "refused (restart the worker deliberately to change the auth profile)")
            raise ConfigError(["auth.mode/audience changes require a worker restart (hot reload refused)"])
        self._register_accounts(cfg)
        self.cfg = cfg
        self.auth = Authenticator(cfg.auth, self.clock)

    # ============================================================ alerts/incidents
    def alert(self, level: str, code: str, message: str, account_id: str | None = None,
              trade_id: str | None = None, **detail: Any) -> None:
        now = self.clock.now()
        # Anti-flood: an identical alert within the dedup window is counted, not re-emitted.
        key = (level, code, account_id, trade_id, message)
        window = self.cfg.execution.alert_dedup_seconds
        mono = now.timestamp()
        seen = self._alert_seen.get(key)
        if seen is not None and window > 0 and mono - seen[0] < window:
            self._alert_seen[key] = (seen[0], seen[1] + 1)
            return
        if seen is not None and seen[1]:
            detail = dict(detail, suppressed_repeats=seen[1])
        self._alert_seen[key] = (mono, 0)
        if len(self._alert_seen) > 5000:
            self._alert_seen.clear()
        self.j.insert("alerts", ts=now, level=level, code=code, account_id=account_id,
                      trade_id=trade_id, message=message, detail=detail)
        rec = {"ts": iso(now), "level": level, "code": code, "account_id": account_id,
               "trade_id": trade_id, "message": message, "detail": detail}
        getattr(log, level.lower() if level.lower() in ("info", "warning", "error", "critical")
                else "info")("%s %s %s", code, account_id or "-", message)
        if self.alert_sink:
            self.alert_sink(rec)

    def incident(self, kind: R, account_id: str, execution_id: int | None, message: str,
                 block: bool = True, **detail: Any) -> None:
        existing = self.j.q1("SELECT id FROM incidents WHERE status='OPEN' AND kind=? AND account_id=? "
                             "AND COALESCE(execution_id,-1)=COALESCE(?,-1)",
                             (str(kind), account_id, execution_id))
        if existing is None:
            self.j.insert("incidents", account_id=account_id, execution_id=execution_id,
                          kind=str(kind), status="OPEN", message=message, detail=detail,
                          created_at=self.clock.now())
            self.alert("CRITICAL" if block else "WARNING", str(kind), message, account_id, **detail)
        if block:
            self.block_account(account_id, f"{kind}: {message}")

    def block_account(self, account_id: str, reason: str) -> None:
        self.j.update("accounts", "account_id", account_id, entries_blocked=1, block_reason=reason,
                      updated_at=self.clock.now())

    def _resolve_exec_incidents(self, ex: dict[str, Any], resolution: str) -> None:
        with self.j.tx() as c:
            c.execute("UPDATE incidents SET status='RESOLVED', resolved_at=?, resolution=? "
                      "WHERE status='OPEN' AND execution_id=?",
                      (iso(self.clock.now()), resolution, ex["id"]))

    # ============================================================ cycle
    def run_cycle(self) -> dict[str, Any]:
        """Stages are isolated: a failure in ingestion or event processing (e.g. a locked or
        failing journal, an unwritable archive) never prevents management of existing exposure."""
        t0 = time.perf_counter()
        self.metrics["last_cycle_started_at"] = iso(self.clock.now())
        self._acct_errors = {}
        errors: dict[str, str] = {}
        if not self.started:
            self.startup_reconcile()
        ingested: list[dict[str, Any]] = []
        for name, fn in (("ingest", self.ingest if self.inbox else None),
                         ("events", self.process_events), ("manage", self.manage_all),
                         ("quotes", self._probe_quotes)):
            if fn is None:
                continue
            try:
                res = fn()
                if name == "ingest":
                    ingested = res  # type: ignore[assignment]
            except Exception as e:  # noqa: BLE001 - recorded, cycle continues with next stage
                log.exception("cycle stage %s failed", name)
                errors[name] = repr(e)
        self.cycles += 1
        if self.cycles % self.cfg.reconcile_every_cycles == 0:
            for aid in list(self.cfg.accounts):
                try:
                    self._reconcile_account(aid, kind="PERIODIC")
                except Exception as e:  # noqa: BLE001
                    errors[f"reconcile:{aid}"] = repr(e)
        self.metrics["last_stage_errors"] = errors
        if errors:
            self.metrics["cycle_errors"] += 1
        self.metrics["last_cycle_completed_at"] = iso(self.clock.now())
        self.metrics["last_cycle_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        return {"cycle": self.cycles, "ingested": ingested, "errors": errors}

    def _probe_quotes(self) -> None:
        """Record per-account reference-quote age (health evidence; not a trading decision)."""
        for aid, acct in self.cfg.accounts.items():
            adapter = self.adapters.get(aid)
            sym = next(iter(acct.instruments.values()), None)
            if adapter is None or sym is None:
                continue
            try:
                ts = adapter.reference_prices(sym).ts
                self.metrics["quote_age_seconds"][aid] = round((self.clock.now() - ts).total_seconds(), 3)
            except AdapterError as e:
                self.metrics["quote_age_seconds"][aid] = None
                self.alert("WARNING", "QUOTE_UNAVAILABLE", f"{aid}: {e}", aid)

    def startup_reconcile(self) -> dict[str, str]:
        """Reconcile every account BEFORE enabling new entries."""
        results: dict[str, str] = {}
        for aid in self.cfg.accounts:
            results[aid] = self._reconcile_account(aid, kind="STARTUP")
        self.manage_all()
        self.started = True
        return results

    def _reconcile_account(self, aid: str, kind: str) -> str:
        adapter = self.adapters.get(aid)
        now = self.clock.now()
        if adapter is None:
            self.ready.discard(aid)
            return "NO_ADAPTER"
        try:
            known = self.j.known_client_ids(aid)
            findings: list[dict[str, Any]] = []
            for pos in adapter.positions():
                ex = self._active_exec_for_symbol(aid, pos.symbol)
                if ex is None:
                    findings.append({"symbol": pos.symbol, "qty": s(pos.qty), "side": str(pos.side)})
                    self.incident(R.UNOWNED_EXPOSURE, aid, None,
                                  f"{pos.side} {pos.qty} {pos.symbol} on {aid} is not owned by any "
                                  "journaled execution; not adopted", symbol=pos.symbol, qty=s(pos.qty))
            for sym in sorted(set(self.cfg.accounts[aid].instruments.values())):
                for o in adapter.open_orders(sym):
                    if o.client_order_id not in known:
                        findings.append({"unknown_order": o.client_order_id})
                        self.incident(R.UNKNOWN_VENUE_ORDER, aid, None,
                                      f"working order {o.client_order_id} on {sym} is unknown to the journal",
                                      client_order_id=o.client_order_id)
            bal = adapter.balance()
            self.j.insert("balance_snapshots", ts=now, account_id=aid, currency=bal.currency,
                          wallet=bal.wallet, available=bal.available)
            self.j.insert("reconciliations", ts=now, account_id=aid, kind=kind,
                          result="FINDINGS" if findings else "OK", detail={"findings": findings})
            self.metrics["last_reconcile_at"][aid] = iso(now)
            self.ready.add(aid)
            return "FINDINGS" if findings else "OK"
        except AdapterError as e:
            self.ready.discard(aid)
            self.j.insert("reconciliations", ts=now, account_id=aid, kind=kind, result="ERROR",
                          detail={"error": str(e)})
            self.alert("ERROR", "RECONCILE_FAILED", f"{aid}: {e}", aid)
            return "ERROR"

    def _active_exec_for_symbol(self, aid: str, symbol: str) -> dict[str, Any] | None:
        ph = ",".join("?" for _ in TERMINAL)
        return self.j.q1(f"SELECT * FROM executions WHERE account_id=? AND native_symbol=? AND "
                         f"state NOT IN ({ph}) ORDER BY id DESC LIMIT 1",
                         (aid, symbol, *sorted(TERMINAL)))

    # ============================================================ ingestion
    def ingest(self) -> list[dict[str, Any]]:
        assert self.inbox is not None
        out = []
        held = 0
        for path in self.inbox.pending():
            try:
                data = path.read_bytes()
            except OSError as e:  # being written / locked: retry next scan
                log.warning("cannot read %s: %s", path, e)
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                self._reject_file(path, R.INVALID_JSON, "file is not valid UTF-8", data)
                out.append({"file": path.name, "outcome": "INVALID", "code": str(R.INVALID_JSON)})
                continue
            try:
                res = self.ingest_text(text, path.name, path)
            except sqlite3.Error:
                raise  # journal unavailable: stop ingestion this cycle (nothing consumed)
            except Exception as e:  # noqa: BLE001 - never let one file stall the inbox
                log.exception("ingest of %s failed", path.name)
                self._reject_file(path, R.INVALID_JSON, f"unprocessable input: {type(e).__name__}", data)
                res = {"file": path.name, "outcome": "INVALID", "code": str(R.INVALID_JSON)}
            held += int(res.get("outcome") == "HELD")
            out.append(res)
        self.metrics["held_files"] = held
        return out

    def _reject_file(self, path: Path, code: R, message: str, data: bytes) -> None:
        self.j.insert("ingest_log", ts=self.clock.now(), file_name=path.name,
                      file_hash=hashlib.sha256(data).hexdigest(), event_id=None, outcome="INVALID",
                      code=str(code), message=message, raw_text=None)
        self.alert("WARNING", "SIGNAL_INVALID", f"{path.name}: {code} {message}")
        self._move_file("quarantine", path, str(code), message)

    def _move_file(self, op: str, path: Path, *args: str) -> None:
        """Archive/quarantine AFTER journaling. A failed move is reported, never fatal: the
        file is re-scanned and recognised as a duplicate of the journaled event."""
        assert self.inbox is not None
        try:
            if op == "archive":
                self.inbox.archive(path)
            else:
                self.inbox.quarantine(path, *args)
        except OSError as e:
            self.alert("ERROR", "INBOX_MOVE_FAILED", f"{op} of {path.name} failed: {e}; will retry")

    def ingest_text(self, text: str, file_name: str, path: Path | None = None) -> dict[str, Any]:
        """Validate + durably journal. Only then archive/quarantine the file."""
        now = self.clock.now()
        fhash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        raw: dict[str, Any] | None = None
        # 1) Authentication + authorization BEFORE any event/trade state is created.
        try:
            authr = self.auth.unwrap(text)
        except AuthRejection as r:
            if r.hold:  # fail closed without consuming the file (e.g. key not configured)
                if self.j.q1("SELECT id FROM ingest_log WHERE file_hash=? AND outcome='HELD'", (fhash,)) is None:
                    self.j.insert("ingest_log", ts=now, file_name=file_name, file_hash=fhash, event_id=None,
                                  outcome="HELD", code=str(r.code), message=r.message, raw_text=None)
                self.alert("ERROR", str(r.code), f"{file_name}: {r.message}")
                return {"file": file_name, "outcome": "HELD", "code": str(r.code)}
            outcome = ("INVALID" if r.code in (R.INVALID_JSON, R.DUPLICATE_JSON_KEY, R.INVALID_NUMBER)
                       else "AUTH_REJECTED")
            self.j.insert("ingest_log", ts=now, file_name=file_name, file_hash=fhash, event_id=None,
                          outcome=outcome, code=str(r.code), message=r.message,
                          raw_text=text[:2000])
            self.alert("WARNING", "SIGNAL_AUTH_REJECTED", f"{file_name}: {r.code} {r.message}")
            if path and self.inbox:
                self._move_file("quarantine", path, str(r.code), r.message)
            return {"file": file_name, "outcome": outcome, "code": str(r.code)}
        try:
            raw = load_event_json(json.dumps(authr.payload)) if authr.mode == "HMAC" else load_event_json(text)
            ev = parse_event(raw)
        except Rejection as r:
            eid = raw.get("event_id") if isinstance(raw, dict) and isinstance(raw.get("event_id"), str) else None
            self.j.insert("ingest_log", ts=now, file_name=file_name, file_hash=fhash, event_id=eid,
                          outcome="INVALID", code=str(r.code), message=r.message, raw_text=text[:20000])
            self.alert("WARNING", "SIGNAL_INVALID", f"{file_name}: {r.code} {r.message}")
            if path and self.inbox:
                self._move_file("quarantine", path, str(r.code), r.message)
            return {"file": file_name, "outcome": "INVALID", "code": str(r.code)}
        chash = content_hash(raw)
        existing = self.j.event_by_id(ev.event_id)
        if existing is not None:
            if existing["content_hash"] == chash:
                outcome, code = "DUPLICATE", R.DUPLICATE_EVENT
            else:
                outcome, code = "CONFLICT", R.EVENT_ID_CONFLICT
            self.j.insert("ingest_log", ts=now, file_name=file_name, file_hash=fhash,
                          event_id=ev.event_id, outcome=outcome, code=str(code),
                          message="same event_id already journaled", raw_text=text[:20000])
            if outcome == "CONFLICT":
                self.alert("ERROR", str(code), f"event_id {ev.event_id} reused with different content",
                           trade_id=ev.trade_id)
                if path and self.inbox:
                    self._move_file("quarantine", path, str(code), "event_id reused with different content")
            elif path and self.inbox:
                self._move_file("archive", path)
            return {"file": file_name, "outcome": outcome, "event_id": ev.event_id}
        with self.j.tx() as c:
            c.execute(
                "INSERT INTO events (event_id, content_hash, trade_id, event_type, revision, producer,"
                " environment, created_at, source_data_time, received_at, source_file, canonical_json,"
                " process_status, auth_mode, auth_key_id, envelope_issued_at, envelope_hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'PENDING',?,?,?,?)",
                (ev.event_id, chash, ev.trade_id, str(ev.event_type), ev.revision, ev.producer_name,
                 str(ev.environment), iso(ev.created_at),
                 iso(ev.source.data_time) if ev.source else None, iso(now), file_name,
                 canonical_json(raw), authr.mode, authr.key_id, iso(authr.issued_at),
                 authr.envelope_hash))
            c.execute("INSERT INTO ingest_log (ts, file_name, file_hash, event_id, outcome) "
                      "VALUES (?,?,?,?, 'JOURNALED')", (iso(now), file_name, fhash, ev.event_id))
        crashpoint("after_ingest_commit")
        if path and self.inbox:
            self._move_file("archive", path)
        return {"file": file_name, "outcome": "JOURNALED", "event_id": ev.event_id}

    # ============================================================ event application
    def process_events(self) -> None:
        for row in self.j.pending_events():
            try:
                ev = parse_event(json.loads(row["canonical_json"]))
                status, code, msg, defer = self._apply_event(ev, row)
            except Rejection as r:
                status, code, msg, defer = "REJECTED", r.code, r.message, None
            except sqlite3.Error:
                raise  # journal failing: stop event processing (retried next cycle), never continue blind
            except Exception as e:  # never let one event stall the cycle; retried next cycle
                log.exception("event %s failed", row["event_id"])
                self.alert("ERROR", str(R.INTERNAL_ERROR), f"event {row['event_id']}: {e!r}; will retry",
                           trade_id=row["trade_id"])
                continue
            self.j.update("events", "seq", row["seq"], process_status=status,
                          process_code=str(code) if code else None, process_message=msg,
                          defer_until=defer, processed_at=self.clock.now())
            if status in ("REJECTED", "STALE"):
                self.alert("WARNING" if status == "REJECTED" else "INFO", str(code),
                           f"{row['event_type']} {row['event_id']}: {msg}", trade_id=row["trade_id"])

    def _event_gate(self, ev: Event) -> None:
        sp = self.cfg.signals
        if ev.environment is Environment.LIVE:
            raise Rejection(R.LIVE_NOT_SUPPORTED,
                            "LIVE events are refused: Phase 1 executes in simulation only")
        if ev.producer_name not in sp.trusted_producers:
            raise Rejection(R.UNTRUSTED_PRODUCER,
                            f"producer {ev.producer_name!r} is not configured as trusted "
                            "(note: a producer name is not authentication)")

    def _authorization_gate(self, ev: Event, row: dict[str, Any]) -> None:
        """Policy for already-journaled events whose key was revoked (or mode tightened) since
        ingestion: commands that authorize or reshape exposure (OPEN, AMEND_EXITS) are refused;
        CLOSE / CANCEL_ENTRY only reduce exposure and remain applied."""
        mode = row.get("auth_mode")
        if mode == "HMAC":
            spec = self.cfg.auth.keys.get(row.get("auth_key_id") or "")
            valid = (spec is not None and spec.active and spec.producer == ev.producer_name
                     and ev.strategy_id in spec.strategies and str(ev.environment) in spec.environments)
            if valid:
                return
            if ev.event_type in (EventType.OPEN, EventType.AMEND_EXITS):
                raise Rejection(R.AUTH_KEY_REVOKED, "signing key revoked/removed or its scope no longer "
                                                    "covers this event (re-checked at apply time)")
            trade = self.j.trade(ev.trade_id)
            if trade is None or trade["open_event_id"] is None:
                # exposure-reducing commands from a no-longer-valid key may still close EXISTING
                # exposure, but must never create a tombstone that blocks a future authorized OPEN
                raise Rejection(R.AUTH_KEY_REVOKED, "CLOSE/CANCEL from a revoked/out-of-scope key for a "
                                                    "trade with no OPEN: no tombstone created")
            return
        if ev.event_type not in (EventType.OPEN, EventType.AMEND_EXITS):
            return
        if self.cfg.auth.mode == "strict":
            raise Rejection(R.AUTH_REQUIRED, "unsigned event ingested under the compatibility "
                                             "profile is not applied in strict mode")

    def _apply_event(self, ev: Event, row: dict[str, Any]) -> tuple[str, Any, str, Any]:
        self._event_gate(ev)
        self._authorization_gate(ev, row)
        trade = self.j.trade(ev.trade_id)
        now = self.clock.now()
        if trade is not None and trade["strategy_id"] and trade["strategy_id"] != ev.strategy_id:
            raise Rejection(R.STRATEGY_MISMATCH, "event strategy_id differs from the trade's")
        if trade is not None and trade["instrument_key"] and trade["instrument_key"] != ev.instrument.key:
            raise Rejection(R.INSTRUMENT_MISMATCH, "event instrument differs from the trade's")

        if ev.event_type is EventType.OPEN:
            return self._apply_open(ev, trade)
        if trade is None or trade["open_event_id"] is None:
            if ev.event_type in (EventType.CLOSE, EventType.CANCEL_ENTRY):
                self._tombstone(ev, trade)
                return ("APPLIED", R.TRADE_TOMBSTONED,
                        f"{ev.event_type} before OPEN: durable tombstone created", None)
            # AMEND without predecessor: bounded hold, then reject.
            received = parse_ts(row["received_at"]) or now
            hold_until = received + timedelta(seconds=self.cfg.signals.missing_predecessor_hold_seconds)
            if trade is not None and trade["status"] == "TOMBSTONED":
                return "REJECTED", R.TRADE_TOMBSTONED, "trade is tombstoned", None
            if now < hold_until:
                return "DEFERRED", R.MISSING_PREDECESSOR, "waiting for OPEN", hold_until
            return ("REJECTED", R.MISSING_PREDECESSOR,
                    "no OPEN arrived within the missing-predecessor hold window", None)

        if ev.event_type is EventType.AMEND_EXITS:
            if ev.revision <= trade["last_revision"]:
                return ("STALE", R.STALE_REVISION,
                        f"revision {ev.revision} <= applied {trade['last_revision']}", None)
            failures = 0
            for ex in self.j.executions_for_trade(ev.trade_id):
                if self._amend_done(ex["id"], ev.event_id):
                    continue
                try:
                    self._amend_execution(ex, ev)
                except AdapterError as e:
                    failures += 1
                    self.alert("ERROR", "ADAPTER_ERROR", f"amendment for execution {ex['id']} deferred: {e}",
                               ex["account_id"], ex["trade_id"])
            if failures:  # stays PENDING; already-amended executions are skipped on retry
                return ("PENDING", R.ADAPTER_UNAVAILABLE,
                        f"{failures} execution(s) not yet amended; retrying next cycle", None)
            self.j.update("trades", "trade_id", ev.trade_id, last_revision=ev.revision, updated_at=now)
            return "APPLIED", None, "amendment applied to recorded executions", None

        # CLOSE / CANCEL_ENTRY are monotone (they only reduce exposure): applied even if
        # their revision is older than an applied amendment; repeated ones are no-ops.
        self.j.update("trades", "trade_id", ev.trade_id,
                      last_revision=max(ev.revision, trade["last_revision"]),
                      status="CLOSE_REQUESTED" if ev.event_type is EventType.CLOSE else
                      ("CANCEL_REQUESTED" if trade["status"] == "ACTIVE" else trade["status"]),
                      updated_at=now)
        effects = 0
        for ex in self.j.executions_for_trade(ev.trade_id):
            if ev.event_type is EventType.CLOSE:
                effects += int(bool(self._safe(ex, lambda e=ex: self._close_execution(e, ev))))
            else:
                effects += int(bool(self._safe(ex, lambda e=ex: self._cancel_execution(e, ev))))
        return "APPLIED", None, f"{effects} execution(s) affected", None

    def _tombstone(self, ev: Event, trade: dict[str, Any] | None) -> None:
        now = self.clock.now()
        if trade is None:
            self.j.insert("trades", trade_id=ev.trade_id, strategy_id=ev.strategy_id,
                          strategy_version=ev.strategy_version, instrument_key=ev.instrument.key,
                          status="TOMBSTONED", last_revision=ev.revision,
                          tombstone_reason=f"{ev.event_type} {ev.event_id} before OPEN",
                          created_at=now, updated_at=now)
        else:
            self.j.update("trades", "trade_id", ev.trade_id, status="TOMBSTONED", updated_at=now,
                          last_revision=max(trade["last_revision"], ev.revision))

    def _amend_done(self, exec_id: int, event_id: str) -> bool:
        return self.j.conn.execute(
            "SELECT 1 FROM execution_log WHERE execution_id=? AND kind IN "
            "('AMEND_APPLIED','AMEND_REJECTED','AMEND_NOOP') AND json_extract(detail,'$.event_id')=?",
            (exec_id, event_id)).fetchone() is not None

    def _safe(self, ex: dict[str, Any], fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except AdapterError as e:
            self.alert("ERROR", "ADAPTER_ERROR", f"execution {ex['id']}: {e}", ex["account_id"],
                       ex["trade_id"])
            return None

    # ------------------------------------------------------------ OPEN
    def _apply_open(self, ev: Event, trade: dict[str, Any] | None) -> tuple[str, Any, str, Any]:
        assert ev.open is not None and ev.source is not None
        now = self.clock.now()
        if trade is not None:
            if trade["status"] == "TOMBSTONED":
                raise Rejection(R.TRADE_TOMBSTONED,
                                "a CLOSE/CANCEL_ENTRY for this trade arrived first; OPEN refused")
            if trade["open_event_id"] != ev.event_id:
                raise Rejection(R.DUPLICATE_OPEN_CONFLICT,
                                "trade already has an OPEN with another event_id")
            # Same OPEN re-processed after an interruption: resume routing to the ORIGINAL
            # recorded accounts that have no execution yet (each still passes full preflight,
            # including signal age/expiry). Accounts already handled are never re-entered.
            done = {e["account_id"] for e in self.j.executions_for_trade(ev.trade_id)}
            pending = [a for a in (trade["routing"] or []) if a not in done]
            if trade["status"] != "ACTIVE":
                pending = []
            for aid in pending:
                self._open_for_account_safe(ev, aid)
            return "APPLIED", None, f"resumed routing for {len(pending)} account(s)", None
        sp = self.cfg.signals
        src_q, tgt_q = ev.source.quote_currency, ev.instrument.quote
        if src_q != tgt_q and (src_q, tgt_q) not in sp.allowed_currency_mappings:
            raise Rejection(R.CURRENCY_MAPPING_UNSUPPORTED,
                            f"source quote {src_q} -> target {tgt_q} is not an allowed mapping "
                            "(USD and USDT are not interchangeable)")
        accounts = self.cfg.routes.get(ev.strategy_id, ())
        if not accounts:
            raise Rejection(R.NO_ROUTE, f"no accounts routed for strategy {ev.strategy_id}")
        # Persist the trade and its ORIGINAL routing before any external action.
        self.j.insert("trades", trade_id=ev.trade_id, strategy_id=ev.strategy_id,
                      strategy_version=ev.strategy_version, instrument_key=ev.instrument.key,
                      direction=str(ev.open.direction), status="ACTIVE", open_event_id=ev.event_id,
                      last_revision=1, routing=list(accounts), config_hash=self.cfg.source_hash,
                      created_at=now, updated_at=now)
        for aid in accounts:
            self._open_for_account_safe(ev, aid)
        return "APPLIED", None, f"routed to {len(accounts)} account(s)", None

    def _open_for_account_safe(self, ev: Event, aid: str) -> None:
        try:
            self._open_for_account(ev, aid)
        except AdapterError as e:
            self.alert("ERROR", "ADAPTER_ERROR", f"{aid}: {e}", aid, ev.trade_id)
        except sqlite3.Error:
            raise  # local persistence failure: abort the event (retried), never continue blind
        except Exception as e:
            log.exception("OPEN %s for %s failed", ev.trade_id, aid)
            self.alert("CRITICAL", str(R.INTERNAL_ERROR), f"{aid}: {e!r}", aid, ev.trade_id)

    def _open_for_account(self, ev: Event, aid: str) -> None:
        assert ev.open is not None
        now = self.clock.now()
        acct = self.cfg.accounts.get(aid)
        base = dict(trade_id=ev.trade_id, account_id=aid, venue=acct.venue if acct else "?",
                    instrument_key=ev.instrument.key, strategy_id=ev.strategy_id,
                    direction=str(ev.open.direction), leverage=ev.open.leverage,
                    policy_snapshot=acct.snapshot() if acct else {},
                    exit_spec=ev.open.exits.to_dict(),
                    holding=ev.open.holding.to_dict() if ev.open.holding else None,
                    source_risk=ev.source_risk.to_dict() if ev.source_risk else None,
                    requested_margin=acct.initial_margin_per_trade if acct else None,
                    created_at=now, updated_at=now)
        try:
            pf = self._preflight(ev, acct, aid)
        except Rejection as r:
            state = ExecState.SKIPPED if r.code in SKIP_CODES else ExecState.REJECTED
            self.j.insert("executions", **base, state=str(state), reason_code=str(r.code),
                          reason_message=r.message)
            self.alert("INFO" if state is ExecState.SKIPPED else "WARNING", str(r.code),
                       f"{state} {ev.trade_id}: {r.message}", aid, ev.trade_id, **r.details)
            return
        except AdapterError as e:
            self.j.insert("executions", **base, state=str(ExecState.REJECTED),
                          reason_code=str(R.ADAPTER_UNAVAILABLE), reason_message=str(e))
            self.alert("ERROR", str(R.ADAPTER_UNAVAILABLE), str(e), aid, ev.trade_id)
            return
        sz = pf.sizing
        fields = dict(
            native_symbol=pf.symbol, instrument_snapshot=pf.rules.to_dict(),
            plan={"expected_fill": s(pf.expected_fill), "sizing_basis":
                  "LONG: IOC ceiling (upper bound of fill price); SHORT: max(best bid, reference "
                  "entry) - not an upper bound on notional",
                  "raw_contracts": s(sz.raw_contracts),
                  "available_at_plan": s(pf.available)},
            target_notional=sz.target_notional, sizing_price=pf.sizing_price,
            limit_price=pf.limit_price, planned_qty=sz.planned_contracts,
            planned_qty_btc=sz.planned_qty_btc, planned_notional=sz.planned_notional,
            planned_margin=sz.planned_margin, fee_reservation=pf.fee_reservation)
        # Atomic local reservation + overlap re-check.
        with self.j.tx() as c:
            others = self._reserved_sum(aid)
            if pf.available - others < pf.reservation:
                c.execute("SELECT 1")  # keep tx shape
                exec_id = self._insert_exec(base, fields, ExecState.REJECTED, R.INSUFFICIENT_COLLATERAL,
                                            f"available {pf.available} - reserved {others} < "
                                            f"required {pf.reservation}")
                rejected = True
            elif self._overlap(aid, ev.instrument.key):
                exec_id = self._insert_exec(base, fields, ExecState.REJECTED, R.OVERLAPPING_TRADE,
                                            "an active trade already exists for this account/instrument")
                rejected = True
            else:
                exec_id = self._insert_exec(base, dict(fields, reserved_amount=pf.reservation),
                                            ExecState.RESERVED, None, None)
                rejected = False
        if rejected:
            ex = self.j.execution(exec_id)
            self.alert("WARNING", ex["reason_code"], ex["reason_message"], aid, ev.trade_id)
            return
        self._submit_entry(self.j.execution(exec_id))

    def _insert_exec(self, base: dict[str, Any], fields: dict[str, Any], state: ExecState,
                     code: R | None, msg: str | None) -> int:
        return self.j.insert("executions", **base, **fields, state=str(state),
                             reason_code=str(code) if code else None, reason_message=msg)

    def _degraded_reasons(self, aid: str) -> list[str]:
        """Dynamic entry gate: clears automatically once coverage is restored."""
        out = []
        for e in self.j.q("SELECT id, state, stop_coverage, tp_coverage FROM executions WHERE "
                          "account_id=? AND state IN ('OPEN_UNPROTECTED','OPEN_PROTECTED','CLOSING')",
                          (aid,)):
            if e["tp_coverage"] == "DEGRADED":
                out.append(f"execution {e['id']} TP coverage degraded")
            if e["stop_coverage"] == "UNVERIFIED":
                out.append(f"execution {e['id']} stop coverage unverified")
        return out

    def _reserved_sum(self, aid: str) -> Decimal:
        rows = self.j.q("SELECT reserved_amount FROM executions WHERE account_id=? AND state IN "
                        "('RESERVED','ENTRY_SUBMITTED','ENTRY_UNKNOWN')", (aid,))
        return sum((r["reserved_amount"] for r in rows), ZERO)

    def _overlap(self, aid: str, instrument_key: str) -> bool:
        ph = ",".join("?" for _ in TERMINAL)
        return self.j.conn.execute(
            f"SELECT COUNT(*) FROM executions WHERE account_id=? AND instrument_key=? AND "
            f"state NOT IN ({ph})", (aid, instrument_key, *sorted(TERMINAL))).fetchone()[0] > 0

    def _preflight(self, ev: Event, acct: AccountConfig | None, aid: str) -> Preflight:
        assert ev.open is not None and ev.source is not None
        op = ev.open
        now = self.clock.now()
        sp = self.cfg.signals
        if acct is None:
            raise Rejection(R.ACCOUNT_UNKNOWN, f"account {aid} is not configured")
        arow = self.j.account(aid) or {}
        if self.j.control("pause_new_entries") == "1":
            raise Rejection(R.ENTRIES_PAUSED, "new entries are paused by the operator")
        if not acct.enabled_for_entries or arow.get("operator_disabled"):
            raise Rejection(R.ACCOUNT_DISABLED, "account is disabled for new entries")
        if arow.get("entries_blocked"):
            raise Rejection(R.ACCOUNT_BLOCKED, f"account blocked: {arow.get('block_reason')}")
        degraded = self._degraded_reasons(aid)
        if degraded:
            raise Rejection(R.ACCOUNT_DEGRADED,
                            "existing exposure on this account is not fully protected/exit-planned: "
                            + "; ".join(degraded))
        if aid not in self.ready:
            raise Rejection(R.ACCOUNT_NOT_RECONCILED, "account not reconciled since startup")
        if acct.strategy_owner != ev.strategy_id:
            raise Rejection(R.STRATEGY_NOT_OWNER,
                            f"account owner is {acct.strategy_owner}, not {ev.strategy_id}")
        symbol = acct.instruments.get(ev.instrument.key)
        if symbol is None:
            raise Rejection(R.INSTRUMENT_NOT_PERMITTED,
                            f"{ev.instrument.key} is not permitted/mapped for {aid}")
        if self._overlap(aid, ev.instrument.key):
            raise Rejection(R.OVERLAPPING_TRADE,
                            "one active trade per account/instrument; no pyramiding/reversal/aggregation")
        if op.exits.level_policy not in acct.allowed_level_policies:
            raise Rejection(R.LEVEL_POLICY_NOT_PERMITTED,
                            f"level policy {op.exits.level_policy} not enabled for {aid}")
        # ---- timing
        age = (now - ev.created_at).total_seconds()
        if age < -sp.max_clock_skew_seconds:
            raise Rejection(R.CLOCK_SKEW, f"created_at is {-age:.1f}s in the future")
        if age > sp.max_signal_age_seconds:
            raise Rejection(R.SIGNAL_TOO_OLD, f"signal age {age:.1f}s > {sp.max_signal_age_seconds}s")
        if now >= op.expires_at:
            raise Rejection(R.SIGNAL_EXPIRED, f"OPEN expired at {iso(op.expires_at)}")
        if ev.source.data_time is not None:
            dage = (now - ev.source.data_time).total_seconds()
            if dage < -sp.max_clock_skew_seconds:
                raise Rejection(R.CLOCK_SKEW, "source data_time is in the future")
            if dage > sp.max_source_data_age_seconds:
                raise Rejection(R.SOURCE_DATA_STALE, f"source data age {dage:.1f}s too old")
        # ---- adapter: metadata + capabilities (refreshed through the boundary)
        adapter = self.adapters.get(aid)
        if adapter is None:
            raise Rejection(R.ADAPTER_UNAVAILABLE, f"no adapter for {aid}")
        rules = adapter.instrument_rules(symbol)
        if (rules.instrument_key != ev.instrument.key or rules.product_type != ev.instrument.product_type
                or rules.settle != acct.margin_currency):
            raise Rejection(R.INSTRUMENT_RULES_MISMATCH,
                            f"venue rules {rules.instrument_key}/{rules.product_type} settle "
                            f"{rules.settle} do not match {ev.instrument.key} / margin "
                            f"{acct.margin_currency}")
        caps = adapter.capabilities()
        if not caps.isolated_margin:
            raise Rejection(R.MARGIN_MODE_UNSUPPORTED,
                            f"{acct.venue} does not offer isolated margin; cross is never used")
        if not (max(1, rules.min_leverage) <= op.leverage <= min(100, rules.max_leverage)):
            raise Rejection(R.LEVERAGE_UNSUPPORTED,
                            f"leverage {op.leverage} outside supported "
                            f"[{max(1, rules.min_leverage)}, {min(100, rules.max_leverage)}]; "
                            "never silently reduced")
        if op.exits.trigger_reference not in caps.trigger_references:
            raise Rejection(R.TRIGGER_REFERENCE_UNSUPPORTED,
                            f"{acct.venue} cannot trigger on {op.exits.trigger_reference}")
        if not (caps.reduce_only and caps.ioc_limit and caps.client_order_ids):
            raise Rejection(R.VENUE_REJECTED, "venue lacks reduce-only/IOC/client-id capabilities")
        tick = rules.tick_size
        if op.exits.level_policy is LevelPolicy.ABSOLUTE:
            lv = [op.exits.stop_price] + [t.price for t in op.exits.take_profits]
            if not levels_are_tick_aligned(tick, *lv):  # type: ignore[arg-type]
                raise Rejection(R.LEVEL_NOT_TICK_ALIGNED,
                                f"absolute SL/TP must be multiples of tick {tick}; levels are never "
                                "silently rebased")
        # ---- fresh book and reference prices
        book = adapter.order_book(symbol)
        refs = adapter.reference_prices(symbol)
        qage = (now - book.ts).total_seconds()
        if qage > self.cfg.execution.max_quote_age_seconds:
            raise Rejection(R.QUOTE_STALE, f"quote age {qage:.1f}s")
        if book.best_bid is None or book.best_ask is None:
            raise Rejection(R.NO_LIQUIDITY, "empty book side")
        mid = (book.best_bid + book.best_ask) / 2
        spread = (book.best_ask - book.best_bid) / mid
        if spread > acct.max_spread_fraction:
            raise Rejection(R.SPREAD_TOO_WIDE, f"spread {spread:.6f} > {acct.max_spread_fraction}")
        direction = op.direction
        limit = entry_limit_price(direction, op.reference_entry, acct.adverse_entry_tolerance, tick)
        levels = book.asks if direction is Direction.LONG else book.bids
        best = levels[0][0]
        if not price_within_bound(direction, best, limit):
            raise Rejection(R.ENTRY_PRICE_ADVERSE,
                            f"best {'ask' if direction is Direction.LONG else 'bid'} {best} is beyond "
                            f"the adverse bound {limit}", best=s(best), bound=s(limit))
        # ---- sizing (conservative, DOWN-rounded)
        sizing_price = limit if direction is Direction.LONG else max(best, op.reference_entry)
        sz = size_position(acct.initial_margin_per_trade, op.leverage, sizing_price,
                           rules.contract_multiplier, rules.qty_step, rules.min_qty)
        fillable, exp_fill = executable_depth(direction, levels, limit, sz.planned_contracts)
        if fillable <= 0 or exp_fill is None:
            raise Rejection(R.NO_LIQUIDITY, "no executable depth inside the bound")
        # ---- geometry vs expected fill and vs current trigger reference
        stop, tps = resolve_levels(direction, op.exits, exp_fill, tick)
        check_geometry(direction, exp_fill, stop, tps)
        ref_px = refs.get(op.exits.trigger_reference)
        check_geometry(direction, ref_px, stop, tps, what=f"current {op.exits.trigger_reference} price")
        allocate_take_profits(sz.planned_contracts, [t.quantity_fraction for t in op.exits.take_profits],
                              rules.qty_step, rules.min_qty)
        # ---- collateral + configured limits (reject only; never adjust SL/leverage/mode)
        fee_rate = caps.taker_fee_rate
        fee_res = sz.planned_notional * fee_rate * 2
        reservation = sz.planned_margin * (1 + acct.collateral_buffer_fraction) + fee_res
        bal = adapter.balance()
        if bal.currency != acct.margin_currency:
            raise Rejection(R.CURRENCY_MAPPING_UNSUPPORTED,
                            f"account collateral {bal.currency} != {acct.margin_currency}")
        if acct.max_notional_hard is not None and sz.planned_notional > acct.max_notional_hard:
            raise Rejection(R.HARD_LIMIT_EXCEEDED, f"planned notional {sz.planned_notional} > "
                                                   f"{acct.max_notional_hard}")
        if acct.max_initial_margin_hard is not None and sz.planned_margin > acct.max_initial_margin_hard:
            raise Rejection(R.HARD_LIMIT_EXCEEDED, f"planned margin {sz.planned_margin} > "
                                                   f"{acct.max_initial_margin_hard}")
        if acct.max_loss_at_stop is not None:
            worst = downside_at_stop(direction, sz.planned_qty_btc, limit, stop)
            if worst > acct.max_loss_at_stop:
                raise Rejection(R.MAX_LOSS_EXCEEDED,
                                f"worst-case loss at stop {worst} > configured {acct.max_loss_at_stop};"
                                " the stop is not moved and leverage is not reduced")
        if bal.available - self._reserved_sum(aid) < reservation:
            raise Rejection(R.INSUFFICIENT_COLLATERAL,
                            f"available {bal.available} < required reservation {reservation}")
        return Preflight(acct, rules, symbol, limit, sizing_price, exp_fill, sz, fee_res, reservation,
                         bal.available, op.exits)

    # ------------------------------------------------------------ entry submission
    def _adapter(self, ex: dict[str, Any]) -> ExchangeAdapter:
        a = self.adapters.get(ex["account_id"])
        if a is None:
            raise AdapterError("NO_ADAPTER", ex["account_id"])
        return a

    def _rules(self, ex: dict[str, Any]) -> InstrumentRules:
        return InstrumentRules.from_dict(ex["instrument_snapshot"])

    def _release(self, ex: dict[str, Any], state: ExecState, **fields: Any) -> None:
        self.j.update_execution(ex["id"], self.clock.now(), state=str(state), reserved_amount=ZERO,
                                **fields)

    def _submit_entry(self, ex: dict[str, Any]) -> None:
        adapter = self._adapter(ex)
        try:
            adapter.set_margin(ex["native_symbol"], MarginMode.ISOLATED, int(ex["leverage"]))
        except AdapterRejected as e:
            code = (R.MARGIN_MODE_UNSUPPORTED if "ISOLATED" in e.code else
                    R.LEVERAGE_UNSUPPORTED if "LEVERAGE" in e.code else R.VENUE_REJECTED)
            self._release(ex, ExecState.REJECTED, reason_code=str(code), reason_message=str(e),
                          unused_allocation=ex["requested_margin"])
            self.alert("WARNING", str(code), f"margin settings refused: {e}", ex["account_id"], ex["trade_id"])
            return
        except AdapterError as e:  # nothing order-capable was sent; safe to reject
            self._release(ex, ExecState.REJECTED, reason_code=str(R.ADAPTER_UNAVAILABLE),
                          reason_message=str(e), unused_allocation=ex["requested_margin"])
            return
        direction = Direction(ex["direction"])
        order = self._create_order(ex, "ENTRY", 0, 0, OrderKind.ENTRY_LIMIT_IOC, direction.entry_side,
                                   ex["planned_qty"], limit_price=ex["limit_price"])
        self.j.update_execution(ex["id"], self.clock.now(), state=str(ExecState.ENTRY_SUBMITTED))
        outcome, report = self._send(order, adapter)
        ex = self.j.execution(ex["id"])
        if outcome == "REJECTED":
            self._release(ex, ExecState.ENTRY_VENUE_REJECTED, entry_outcome="VENUE_REJECTED",
                          reason_code=str(R.VENUE_REJECTED), reason_message=self.j.order(order["id"])["reject_reason"],
                          unused_allocation=ex["requested_margin"])
            self.alert("WARNING", str(R.VENUE_REJECTED), "entry rejected by venue", ex["account_id"], ex["trade_id"])
        elif outcome == "UNKNOWN":
            self.j.update_execution(ex["id"], self.clock.now(), state=str(ExecState.ENTRY_UNKNOWN),
                                    entry_outcome="UNKNOWN")
            self.alert("WARNING", "ENTRY_OUTCOME_UNKNOWN",
                       "entry submission timed out; reconciling by client order id - no resubmission",
                       ex["account_id"], ex["trade_id"])
        else:
            self._reconcile_entry(ex)

    def _create_order(self, ex: dict[str, Any], leg: str, leg_index: int, plan_version: int,
                      kind: OrderKind, side: Any, qty: Decimal, *, limit_price: Decimal | None = None,
                      trigger_price: Decimal | None = None, trigger_reference: str | None = None,
                      reduce_only: bool = False) -> dict[str, Any]:
        """Persist the intent and deterministic client id BEFORE submission."""
        now = self.clock.now()
        attempt = self.j.next_attempt(ex["id"], leg, leg_index, plan_version)
        cid = client_order_id(ex["trade_id"], ex["account_id"], leg, leg_index, plan_version, attempt)
        oid = self.j.insert("orders", execution_id=ex["id"], account_id=ex["account_id"],
                            native_symbol=ex["native_symbol"], client_order_id=cid, leg=leg,
                            leg_index=leg_index, plan_version=plan_version, attempt=attempt,
                            kind=str(kind), side=str(side), qty=qty, limit_price=limit_price,
                            trigger_price=trigger_price, trigger_reference=trigger_reference,
                            reduce_only=int(reduce_only), status="INTENT", created_at=now,
                            updated_at=now)
        crashpoint(f"after_intent:{leg}", f"after_intent:{leg}:v{plan_version}")
        return self.j.order(oid)

    def _send(self, order: dict[str, Any], adapter: ExchangeAdapter) -> tuple[str, OrderReport | None]:
        req = OrderRequest(order["client_order_id"], order["native_symbol"],
                           Side(order["side"]),
                           OrderKind(order["kind"]), order["qty"], order["limit_price"],
                           order["trigger_price"],
                           PriceRef(order["trigger_reference"]) if order["trigger_reference"] else None,
                           bool(order["reduce_only"]))
        now = self.clock.now()
        try:
            report = adapter.place_order(req)
        except AdapterRejected as e:
            self.j.update("orders", "id", order["id"], status="REJECTED", reject_reason=str(e), updated_at=now)
            self.j.insert("submissions", order_id=order["id"], ts=now, op="PLACE", outcome="REJECTED",
                          detail={"error": str(e)})
            return "REJECTED", None
        except AdapterError as e:
            self.j.update("orders", "id", order["id"], status="UNKNOWN", updated_at=now)
            self.j.insert("submissions", order_id=order["id"], ts=now, op="PLACE", outcome="UNKNOWN",
                          detail={"error": str(e)})
            return "UNKNOWN", None
        crashpoint(f"after_ack:{order['leg']}", f"after_ack:{order['leg']}:v{order['plan_version']}")
        self.j.insert("submissions", order_id=order["id"], ts=now, op="PLACE", outcome="ACK",
                      detail={"status": str(report.status), "venue_order_id": report.venue_order_id})
        self._apply_report(order, report)
        return "ACK", report

    def _apply_report(self, order: dict[str, Any], report: OrderReport) -> None:
        now = self.clock.now()
        new = 0
        for f in report.fills:
            new += int(self.j.add_fill(account_id=order["account_id"], venue_fill_id=f.fill_id,
                                       order_id=order["id"], execution_id=order["execution_id"],
                                       leg=order["leg"], qty=f.qty, price=f.price, fee=f.fee,
                                       fee_currency=f.fee_currency, ts=f.ts))
        # Fill totals come from DE-DUPLICATED journal fills, not from the raw report.
        rows = self.j.q("SELECT qty, price FROM fills WHERE order_id=?", (order["id"],))
        fq, avg = vwap((r["qty"], r["price"]) for r in rows)
        self.j.update("orders", "id", order["id"], status=str(report.status),
                      venue_order_id=report.venue_order_id, filled_qty=fq, avg_price=avg,
                      qty=report.qty, trigger_price=report.trigger_price or order["trigger_price"],
                      not_found_checks=0, updated_at=now)

    def _sync_order(self, order: dict[str, Any], adapter: ExchangeAdapter) -> dict[str, Any]:
        if order["status"] not in LIVE_ORDER:
            return order
        report = adapter.get_order(order["native_symbol"], order["client_order_id"])
        if report is None:
            if order["status"] == "OPEN":
                self.j.update("orders", "id", order["id"], status="UNKNOWN", updated_at=self.clock.now())
            self.j.update("orders", "id", order["id"], not_found_checks=order["not_found_checks"] + 1,
                          updated_at=self.clock.now())
        else:
            self._apply_report(order, report)
        return self.j.order(order["id"])

    # ------------------------------------------------------------ entry reconciliation
    def _reconcile_entry(self, ex: dict[str, Any]) -> None:
        adapter = self._adapter(ex)
        orders = [o for o in self.j.orders_for(ex["id"]) if o["leg"] == "ENTRY"]
        if not orders:
            if ExecState(ex["state"]) is ExecState.RESERVED:
                # Crash between reservation and intent: never submit late.
                self._release(ex, ExecState.ENTRY_CANCELLED, reason_code=str(R.RECOVERY_NOT_SUBMITTED),
                              reason_message="recovered RESERVED without a persisted entry intent; "
                                             "not submitted late", unused_allocation=ex["requested_margin"])
            return
        o = self._sync_order(orders[-1], adapter)
        if o["status"] == "OPEN":  # no resting entries: cancel and re-check next cycle
            try:
                adapter.cancel_order(o["native_symbol"], o["client_order_id"])
            except AdapterError as e:
                self.alert("ERROR", "ENTRY_CANCEL_FAILED", str(e), ex["account_id"], ex["trade_id"])
            self._sync_order(o, adapter)
            return
        if o["status"] in ("FILLED", "CANCELLED", "EXPIRED"):
            self._finalize_entry(self.j.execution(ex["id"]))
            return
        if o["status"] == "REJECTED":
            self._release(ex, ExecState.ENTRY_VENUE_REJECTED, entry_outcome="VENUE_REJECTED",
                          unused_allocation=ex["requested_margin"])
            return
        # still INTENT/UNKNOWN and not found
        self.j.update_execution(ex["id"], self.clock.now(), state=str(ExecState.ENTRY_UNKNOWN),
                                entry_outcome="UNKNOWN")
        if o["not_found_checks"] >= self.cfg.execution.unknown_resolution_checks:
            pos = adapter.position(ex["native_symbol"])
            if pos.qty == 0:
                self.j.update("orders", "id", o["id"], status="NOT_ACCEPTED", updated_at=self.clock.now())
                self._release(ex, ExecState.ENTRY_NOT_ACCEPTED, entry_outcome="NOT_ACCEPTED",
                              reason_message="order never appeared on the venue and no exposure exists",
                              unused_allocation=ex["requested_margin"])
                self.alert("WARNING", "ENTRY_NOT_ACCEPTED", "unknown entry resolved as not accepted",
                           ex["account_id"], ex["trade_id"])
            else:
                self.incident(R.UNATTRIBUTED_EXPOSURE, ex["account_id"], ex["id"],
                              "entry order not found but exposure exists; emergency closing")
                self.j.update("orders", "id", o["id"], status="NOT_ACCEPTED", updated_at=self.clock.now())
                rules = self._rules(ex)
                self.j.update_execution(ex["id"], self.clock.now(), filled_qty=pos.qty,
                                        filled_qty_btc=pos.qty * rules.contract_multiplier,
                                        avg_fill=pos.avg_price, actual_margin=pos.margin,
                                        entry_outcome="UNATTRIBUTED", reserved_amount=ZERO)
                self._begin_close(self.j.execution(ex["id"]), "EMERGENCY_UNATTRIBUTED_EXPOSURE")

    def _finalize_entry(self, ex: dict[str, Any]) -> None:
        """Entry IOC is terminal: record actual fills and economics, then protect."""
        now = self.clock.now()
        rules = self._rules(ex)
        fills = self.j.fills_for(ex["id"], ("ENTRY",))
        qty, avg = vwap((f["qty"], f["price"]) for f in fills)
        if qty == 0:
            state = ExecState.ENTRY_CANCELLED if ex["cancel_requested"] else ExecState.ENTRY_NOT_FILLED
            self._release(ex, state, entry_outcome="NO_FILL", unused_allocation=ex["requested_margin"],
                          reason_message="IOC accepted; nothing filled inside the bound")
            self.alert("INFO", "ENTRY_NOT_FILLED", "IOC accepted but not filled", ex["account_id"], ex["trade_id"])
            return
        assert avg is not None
        lev = Decimal(int(ex["leverage"]))
        qbtc = qty * rules.contract_multiplier
        notional = qbtc * avg
        margin = notional / lev
        fees = sum((f["fee"] for f in fills), ZERO)
        first = parse_ts(fills[0]["ts"])
        holding = HoldingRule.from_dict(ex["holding"])
        deadline = holding.deadline(first) if holding else None
        outcome = "FULL_FILL" if qty == ex["planned_qty"] else "PARTIAL_FILL"
        self.j.update_execution(ex["id"], now, state=str(ExecState.OPEN_UNPROTECTED),
                                entry_outcome=outcome, filled_qty=qty, filled_qty_btc=qbtc,
                                avg_fill=avg, filled_notional=notional, actual_margin=margin,
                                entry_fees=fees, unused_allocation=ex["requested_margin"] - margin,
                                reserved_amount=ZERO, first_fill_at=first, deadline_at=deadline,
                                stop_coverage="NONE", tp_coverage="NONE",
                                unprotected_since=ex["unprotected_since"] or first)
        if ex["planned_margin"] is not None and margin > ex["planned_margin"]:
            # Expected for favourable higher-price SHORT fills (a sell floor is not an upper
            # notional bound) or a book that moved between quote and match. Reported, never
            # "fixed" by changing leverage, stop or configured capital.
            self.alert("WARNING", "ACTUAL_MARGIN_ABOVE_PLAN",
                       f"actual margin {margin} > planned {ex['planned_margin']} "
                       f"(fill {avg} vs sizing price {ex['sizing_price']})",
                       ex["account_id"], ex["trade_id"], actual=s(margin), planned=s(ex["planned_margin"]))
        if outcome == "PARTIAL_FILL":
            self.alert("INFO", "ENTRY_PARTIAL_FILL",
                       f"filled {qty}/{ex['planned_qty']}; remainder cancelled, not chased or topped up",
                       ex["account_id"], ex["trade_id"])
        ex = self.j.execution(ex["id"])
        acct = ex["policy_snapshot"]
        hard_m = D(acct["max_initial_margin_hard"]) if acct.get("max_initial_margin_hard") else None
        hard_n = D(acct["max_notional_hard"]) if acct.get("max_notional_hard") else None
        if (hard_m is not None and margin > hard_m) or (hard_n is not None and notional > hard_n):
            self.incident(R.HARD_LIMIT_BREACH, ex["account_id"], ex["id"],
                          f"actual margin {margin} / notional {notional} breach configured hard "
                          "limits; emergency closing (leverage unchanged, no extra collateral)",
                          margin=s(margin), notional=s(notional))
            self._begin_close(ex, "EMERGENCY_HARD_LIMIT_BREACH")
            return
        try:
            plan = self._build_plan(ex, ExitSpec.from_dict(ex["exit_spec"]), qty, avg, 1, post_fill=True)
        except Rejection as r:
            kind = (R.EXIT_PLAN_INFEASIBLE if r.code in (R.TP_ALLOCATION_INFEASIBLE, R.QTY_BELOW_MINIMUM)
                    else R.POST_FILL_GEOMETRY_INVALID)
            self.incident(kind, ex["account_id"], ex["id"], f"{r.message}; emergency closing",
                          block=kind is R.POST_FILL_GEOMETRY_INVALID)
            self._begin_close(ex, f"EMERGENCY_{kind}")
            return
        self._store_plan(ex, plan)
        self._ensure_protection(self.j.execution(ex["id"]))

    def _build_plan(self, ex: dict[str, Any], spec: ExitSpec, base_qty: Decimal, anchor: Decimal,
                    version: int, post_fill: bool) -> dict[str, Any]:
        rules = self._rules(ex)
        direction = Direction(ex["direction"])
        stop, tps = resolve_levels(direction, spec, anchor, rules.tick_size)
        if post_fill:
            check_geometry(direction, anchor, stop, tps, R.POST_FILL_GEOMETRY_INVALID,
                           R.POST_FILL_GEOMETRY_INVALID, "actual average fill")
        ref = self._adapter(ex).reference_prices(ex["native_symbol"]).get(spec.trigger_reference)
        check_geometry(direction, ref, stop, tps, R.POST_FILL_GEOMETRY_INVALID,
                       R.POST_FILL_GEOMETRY_INVALID, f"current {spec.trigger_reference} price")
        if base_qty < rules.min_qty:
            raise Rejection(R.QTY_BELOW_MINIMUM, f"position {base_qty} below minimum {rules.min_qty}")
        qtys = allocate_take_profits(base_qty, [t.quantity_fraction for t in spec.take_profits],
                                     rules.qty_step, rules.min_qty)
        return {"version": version, "level_policy": str(spec.level_policy),
                "trigger_reference": str(spec.trigger_reference), "stop": s(stop),
                "anchor": s(anchor), "basis_qty": s(base_qty),
                "legs": [{"index": i, "price": s(p), "fraction": s(t.quantity_fraction), "qty": s(q)}
                         for i, (p, t, q) in enumerate(zip(tps, spec.take_profits, qtys))]}

    def _store_plan(self, ex: dict[str, Any], plan: dict[str, Any]) -> None:
        direction = Direction(ex["direction"])
        rules = self._rules(ex)
        stop = D(plan["stop"])
        qbtc = D(plan["basis_qty"]) * rules.contract_multiplier
        avg = ex["avg_fill"]
        risk = downside_at_stop(direction, qbtc, avg, stop)
        pnl = pnl_at_stop(direction, qbtc, avg, stop)
        margin = ex["actual_margin"]
        pct = (risk / margin * 100) if margin else None
        self.j.update_execution(ex["id"], self.clock.now(), exit_plan=plan, risk_at_stop=risk,
                                pnl_at_stop=pnl, risk_pct_of_margin=pct)
        self.j.log_execution(ex["id"], self.clock.now(), "EXIT_PLAN", plan=plan)

    # ------------------------------------------------------------ protection
    def _leg_progress(self, ex: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, Any]]:
        """Durable per-leg executed/remaining quantities derived from journaled TP fills of the
        CURRENT plan version (an amended plan starts its own legs; history stays in the log)."""
        v = int(plan["version"])
        executed: dict[int, Decimal] = {}
        for r in self.j.q("SELECT o.leg_index AS idx, f.qty AS qty FROM fills f JOIN orders o ON "
                          "o.id=f.order_id WHERE f.execution_id=? AND f.leg='TP' AND o.plan_version=?",
                          (ex["id"], v)):
            executed[int(r["idx"])] = executed.get(int(r["idx"]), ZERO) + r["qty"]
        out = []
        for leg in plan["legs"]:
            q, e = D(leg["qty"]), executed.get(int(leg["index"]), ZERO)
            out.append(dict(leg, executed_qty=s(e), remaining_qty=s(max(ZERO, q - e))))
        return out

    def _mark_unprotected(self, ex: dict[str, Any], coverage: str) -> None:
        fields: dict[str, Any] = {"stop_coverage": coverage}
        if not ex["unprotected_since"]:
            fields["unprotected_since"] = self.clock.now()
        if ex["stop_coverage"] != coverage or "unprotected_since" in fields:
            self.j.update_execution(ex["id"], self.clock.now(), **fields)

    def _mark_stop_verified(self, ex: dict[str, Any]) -> None:
        now = self.clock.now()
        since = parse_ts(ex["unprotected_since"])
        if since is not None:
            dur = Decimal(str(round((now - since).total_seconds(), 3)))
            prev = ex["max_unprotected_seconds"] or ZERO
            self.j.log_execution(ex["id"], now, "UNPROTECTED_INTERVAL", seconds=s(dur))
            self.j.update_execution(ex["id"], now, stop_coverage="VERIFIED", unprotected_since=None,
                                    max_unprotected_seconds=max(prev, dur))
        elif ex["stop_coverage"] != "VERIFIED":
            self.j.update_execution(ex["id"], now, stop_coverage="VERIFIED")

    def _backoff(self, n: int) -> timedelta:
        ec = self.cfg.execution
        return timedelta(seconds=min(ec.tp_retry_base_seconds * 2 ** max(0, n - 1), ec.tp_retry_max_seconds))

    def _ensure_protection(self, ex: dict[str, Any]) -> None:
        """Converge exchange-held protection to the desired state for ACTUAL exposure.
        Desired vs observed is compared first; venue writes happen only when they differ, so a
        healthy unchanged position produces no order traffic (no cancel/re-create churn)."""
        adapter = self._adapter(ex)
        now = self.clock.now()
        for o in self.j.orders_for(ex["id"]):
            if o["leg"] in EXIT_LEGS:
                self._sync_order(o, adapter)
        pos = adapter.position(ex["native_symbol"])
        ex = self.j.execution(ex["id"])
        if pos.qty == 0:
            self._handle_flat(ex)
            return
        direction = Direction(ex["direction"])
        if pos.side is not direction:
            self.incident(R.POSITION_MISMATCH, ex["account_id"], ex["id"],
                          f"venue position side {pos.side} != {direction}")
            return
        plan = ex["exit_plan"]
        if plan is None:
            return
        v = int(plan["version"])
        rules = self._rules(ex)
        threshold = self.cfg.execution.unknown_resolution_checks
        # Unresolved protective orders: one missing observation is not proof of absence.
        pending_stop = False
        pending_tp: set[int] = set()
        for o in self.j.orders_for(ex["id"]):
            if o["leg"] in ("STOP", "TP") and o["status"] in UNRESOLVED_ORDER:
                if o["not_found_checks"] >= threshold:
                    self.j.update("orders", "id", o["id"], status="NOT_ACCEPTED", updated_at=now)
                    self.j.log_execution(ex["id"], now, "ORDER_NOT_ACCEPTED", client_order_id=o["client_order_id"],
                                         checks=o["not_found_checks"])
                elif o["leg"] == "STOP":
                    pending_stop = True
                elif o["plan_version"] == v:
                    pending_tp.add(int(o["leg_index"]))
        orders = self.j.orders_for(ex["id"])
        trig_ref = plan["trigger_reference"]
        opens = [o for o in orders if o["leg"] == "STOP" and o["status"] == "OPEN"]
        current = [o for o in opens if o["plan_version"] == v]
        covering = any(o["qty"] >= pos.qty for o in opens)
        if not current:
            if pending_stop:
                if covering:
                    self._mark_stop_verified(ex)
                else:
                    self._mark_unprotected(ex, "UNVERIFIED")
                self.alert("WARNING", "PROTECTION_UNVERIFIED",
                           "stop placement outcome not yet observed on venue; re-checking (bounded) "
                           "before any replacement", ex["account_id"], ex["trade_id"])
                return
            ok, o = self._place_protective(ex, adapter, "STOP", 0, v, OrderKind.STOP_MARKET, pos.qty,
                                           D(plan["stop"]), trig_ref)
            if not ok:
                if o is not None and o["status"] in UNRESOLVED_ORDER:
                    if covering:
                        self._mark_stop_verified(self.j.execution(ex["id"]))
                    else:
                        self._mark_unprotected(self.j.execution(ex["id"]), "UNVERIFIED")
                return
            current = [o]  # type: ignore[list-item]
        else:
            exact = [o for o in current if o["qty"] == pos.qty]
            st = exact[-1] if exact else current[-1]  # best fit, newest first
            current = [st] + [o for o in current if o["id"] != st["id"]]
            if st["qty"] < pos.qty:  # stop smaller than exposure: coverage NOT verified - fix now
                if not self._resize_stop(ex, adapter, st, pos.qty):
                    self._protection_failure(ex, f"stop {st['qty']} smaller than exposure {pos.qty} "
                                                 "and could not be resized")
                    return
            elif st["qty"] > pos.qty:  # oversize reduce-only stop still covers: cosmetic, bounded retry
                retry_at = parse_ts(ex["stop_fix_retry_at"])
                if retry_at is None or now >= retry_at:
                    if self._resize_stop(ex, adapter, st, pos.qty):
                        self.j.update_execution(ex["id"], now, stop_fix_retry_at=None)
                    else:
                        self.j.update_execution(ex["id"], now, stop_fix_retry_at=now + self._backoff(2))
                        self.alert("WARNING", "STOP_RESIZE_DEFERRED",
                                   f"stop qty {st['qty']} > position {pos.qty}; reduce-only stop still "
                                   "covers the exposure; resize retried later", ex["account_id"], ex["trade_id"])
            for extra in current[1:]:
                self._cancel(extra, adapter)
        for o in opens:
            if o["plan_version"] != v:  # only after the current-version stop is verified OPEN
                self._cancel(o, adapter)
        ex = self.j.execution(ex["id"])
        self._mark_stop_verified(ex)
        ex = self.j.execution(ex["id"])
        # ---- take-profit legs (stop already verified: failures are DEGRADED, never a forced close)
        for o in self.j.orders_for(ex["id"]):
            if o["leg"] == "TP" and o["status"] == "OPEN" and o["plan_version"] != v:
                self._cancel(o, adapter)
        pos = adapter.position(ex["native_symbol"])  # re-read: an old TP may have filled meanwhile
        if pos.qty == 0:
            self._handle_flat(self.j.execution(ex["id"]))
            return
        progress = self._leg_progress(ex, plan)
        complete, failed = True, False
        retry_at = parse_ts(ex["tp_retry_at"])
        orders = self.j.orders_for(ex["id"])
        budget = pos.qty - sum((o["qty"] for o in orders if o["leg"] == "TP" and o["status"] == "OPEN"
                                and o["plan_version"] == v), ZERO)
        for leg in progress:
            i = int(leg["index"])
            mine = [o for o in orders if o["leg"] == "TP" and o["leg_index"] == i and o["plan_version"] == v]
            if any(o["status"] == "OPEN" for o in mine):
                continue
            if i in pending_tp:
                complete = False
                continue
            rem = min(D(leg["remaining_qty"]), pos.qty, max(budget, ZERO))
            if rem <= 0 or rem < rules.min_qty:
                continue  # leg done (or residual below venue minimum: covered by the stop)
            if retry_at is not None and now < retry_at:
                complete = False
                continue
            ok, o = self._place_protective(ex, adapter, "TP", i, v, OrderKind.TAKE_PROFIT_MARKET, rem,
                                           D(leg["price"]), trig_ref, mandatory=False)
            if ok:
                budget -= rem
            if not ok:
                complete = False
                failed = failed or not (o is not None and o["status"] in UNRESOLVED_ORDER)
        self._cancel_strays(ex, adapter)
        new_plan = dict(plan, legs=self._leg_progress(ex, plan))
        if new_plan != plan:
            self.j.update_execution(ex["id"], now, exit_plan=new_plan)
        self._update_tp_coverage(self.j.execution(ex["id"]), complete, failed)
        ex = self.j.execution(ex["id"])
        if ExecState(ex["state"]) is not ExecState.OPEN_PROTECTED or ex["protected_qty"] != pos.qty:
            self.j.update_execution(ex["id"], now, state=str(ExecState.OPEN_PROTECTED), protected_qty=pos.qty)
            self.j.log_execution(ex["id"], now, "PROTECTED", qty=s(pos.qty), plan_version=v)

    def _update_tp_coverage(self, ex: dict[str, Any], complete: bool, failed: bool) -> None:
        now = self.clock.now()
        if complete:
            if ex["tp_coverage"] != "COMPLETE" or ex["tp_fail_count"]:
                if ex["tp_coverage"] == "DEGRADED":
                    self.alert("INFO", "TP_COVERAGE_RESTORED", "take-profit coverage complete again",
                               ex["account_id"], ex["trade_id"])
                self.j.update_execution(ex["id"], now, tp_coverage="COMPLETE", tp_fail_count=0, tp_retry_at=None)
                with self.j.tx() as c:
                    c.execute("UPDATE incidents SET status='RESOLVED', resolved_at=?, resolution=? WHERE "
                              "status='OPEN' AND execution_id=? AND kind=?",
                              (iso(now), "TP coverage restored", ex["id"], str(R.TP_PLACEMENT_PERSISTENT)))
            return
        fields: dict[str, Any] = {"tp_coverage": "DEGRADED"}
        if failed:
            n = int(ex["tp_fail_count"] or 0) + 1
            fields.update(tp_fail_count=n, tp_retry_at=now + self._backoff(n))
            if n == 1:
                self.alert("WARNING", "TP_DEGRADED",
                           "take-profit placement failed; stop coverage verified, position retained; "
                           "new entries on this account are gated until restored", ex["account_id"], ex["trade_id"])
            if n == self.cfg.execution.tp_escalate_after_failures:
                self.incident(R.TP_PLACEMENT_PERSISTENT, ex["account_id"], ex["id"],
                              f"take-profit placement failed {n} times (bounded backoff); stop-protected, "
                              "operator attention required", block=False)
        self.j.update_execution(ex["id"], now, **fields)

    def _place_protective(self, ex: dict[str, Any], adapter: ExchangeAdapter, leg: str, idx: int,
                          version: int, kind: OrderKind, qty: Decimal, trigger: Decimal,
                          trig_ref: str, mandatory: bool = True) -> tuple[bool, dict[str, Any] | None]:
        """Returns (verified_open, order). A timeout whose single immediate re-query does not
        find the order leaves it UNKNOWN (bounded re-checks next cycles) - it is neither treated
        as absent nor resubmitted under a new identity."""
        order = self._create_order(ex, leg, idx, version, kind, Direction(ex["direction"]).exit_side,
                                   qty, trigger_price=trigger, trigger_reference=trig_ref,
                                   reduce_only=True)
        outcome, _ = self._send(order, adapter)
        if outcome == "UNKNOWN":
            order = self._sync_order(self.j.order(order["id"]), adapter)
        order = self.j.order(order["id"])
        if order["status"] == "OPEN":
            return True, order
        if order["status"] in UNRESOLVED_ORDER:
            self.alert("WARNING", "PROTECTION_UNVERIFIED",
                       f"{leg} placement outcome unknown; re-checking by client id", ex["account_id"], ex["trade_id"])
            return False, order
        detail = f"{leg} placement {outcome}/{order['status']} ({order.get('reject_reason')})"
        if mandatory:
            self._protection_failure(ex, detail)
        else:
            self.alert("WARNING", "TP_PLACEMENT_FAILED", f"{detail}; stop remains verified, retry with backoff",
                       ex["account_id"], ex["trade_id"])
        return False, order

    def _resize_stop(self, ex: dict[str, Any], adapter: ExchangeAdapter, stop: dict[str, Any],
                     qty: Decimal) -> bool:
        if adapter.capabilities().amend_protective:
            try:
                rep = adapter.amend_order(stop["native_symbol"], stop["client_order_id"], qty=qty)
                self._apply_report(stop, rep)
                self.j.log_execution(ex["id"], self.clock.now(), "STOP_RESIZED", qty=s(qty))
                return True
            except AdapterError as e:
                refreshed = self._sync_order(stop, adapter)
                if refreshed["status"] == "OPEN" and refreshed["qty"] == qty:
                    return True
                self.alert("WARNING", "STOP_AMEND_FAILED", str(e), ex["account_id"], ex["trade_id"])
        # place-new-then-cancel-old: reduce-only means overlap cannot reverse exposure. The old
        # stop stays in place (still covering) if the replacement cannot be verified.
        ok, _ = self._place_protective(ex, adapter, "STOP", 0, int(stop["plan_version"]),
                                       OrderKind.STOP_MARKET, qty, stop["trigger_price"],
                                       stop["trigger_reference"], mandatory=False)
        if not ok:
            return False
        self._cancel(stop, adapter)
        return True

    def _cancel(self, order: dict[str, Any], adapter: ExchangeAdapter) -> bool:
        try:
            rep = adapter.cancel_order(order["native_symbol"], order["client_order_id"])
            self._apply_report(order, rep)
            return True
        except AdapterError:
            o = self._sync_order(order, adapter)
            return o["status"] not in LIVE_ORDER

    def _cancel_strays(self, ex: dict[str, Any], adapter: ExchangeAdapter) -> None:
        """Cancel venue orders of this execution that the journal no longer considers live."""
        mine = {o["client_order_id"]: o for o in self.j.orders_for(ex["id"])}
        for rep in adapter.open_orders(ex["native_symbol"]):
            o = mine.get(rep.client_order_id)
            if o is not None and o["status"] in UNRESOLVED_ORDER:
                self._apply_report(o, rep)  # it exists: record it, do not cancel inside the window
                continue
            if o is not None and o["status"] == "NOT_ACCEPTED":
                try:
                    self._apply_report(o, adapter.cancel_order(ex["native_symbol"], rep.client_order_id))
                except AdapterError:
                    self._sync_any(o, adapter)  # e.g. it just filled: journal its fills

    def _protection_failure(self, ex: dict[str, Any], detail: str) -> None:
        adapter = self._adapter(ex)
        for o in self.j.orders_for(ex["id"]):  # cancel any unfilled entry remainder
            if o["leg"] == "ENTRY" and o["status"] == "OPEN":
                self._cancel(o, adapter)
        self.incident(R.PROTECTION_FAILED, ex["account_id"], ex["id"],
                      f"protection failed or unverifiable: {detail}; account blocked, emergency close")
        self._begin_close(self.j.execution(ex["id"]), "EMERGENCY_PROTECTION_FAILED")

    # ------------------------------------------------------------ closing
    def _begin_close(self, ex: dict[str, Any], reason: str) -> None:
        if ExecState(ex["state"]) in TERMINAL:
            return
        if ExecState(ex["state"]) is not ExecState.CLOSING:
            self.j.update_execution(ex["id"], self.clock.now(), state=str(ExecState.CLOSING),
                                    close_reason=reason, reserved_amount=ZERO)
            self.j.log_execution(ex["id"], self.clock.now(), "CLOSE_REQUESTED", reason=reason)
        self._progress_close(self.j.execution(ex["id"]))

    def _progress_close(self, ex: dict[str, Any]) -> None:
        adapter = self._adapter(ex)
        for o in self.j.orders_for(ex["id"]):
            if o["leg"] in EXIT_LEGS:
                self._sync_order(o, adapter)
        pos = adapter.position(ex["native_symbol"])
        if pos.qty == 0:
            self._handle_flat(self.j.execution(ex["id"]))
            return
        if pos.side is not Direction(ex["direction"]):
            self.incident(R.POSITION_MISMATCH, ex["account_id"], ex["id"],
                          f"venue position side {pos.side} != {ex['direction']}; manual intervention "
                          "required (no reduce-only close possible)")
            return
        closes = [o for o in self.j.orders_for(ex["id"]) if o["leg"] == "CLOSE"]
        if closes and closes[-1]["status"] in UNRESOLVED_ORDER:
            last = closes[-1]
            if last["not_found_checks"] < self.cfg.execution.unknown_resolution_checks:
                return  # outcome still ambiguous; never assume a timeout means no close
            self.j.update("orders", "id", last["id"], status="NOT_ACCEPTED", updated_at=self.clock.now())
        # keep stop coverage aligned with remaining exposure while closing
        stops = [o for o in self.j.orders_for(ex["id"]) if o["leg"] == "STOP" and o["status"] == "OPEN"]
        if stops and stops[-1]["qty"] != pos.qty and adapter.capabilities().amend_protective:
            try:
                self._apply_report(stops[-1], adapter.amend_order(
                    ex["native_symbol"], stops[-1]["client_order_id"], qty=pos.qty))
            except AdapterError:
                pass
        order = self._create_order(ex, "CLOSE", 0, 0, OrderKind.CLOSE_MARKET,
                                   Direction(ex["direction"]).exit_side, pos.qty, reduce_only=True)
        outcome, _ = self._send(order, adapter)
        if outcome == "REJECTED":
            self.alert("ERROR", "CLOSE_REJECTED", f"close attempt rejected: "
                       f"{self.j.order(order['id'])['reject_reason']}; will retry",
                       ex["account_id"], ex["trade_id"])
        elif outcome == "UNKNOWN":
            self.alert("WARNING", "CLOSE_OUTCOME_UNKNOWN", "close timed out; reconciling",
                       ex["account_id"], ex["trade_id"])
            return
        pos = adapter.position(ex["native_symbol"])
        if pos.qty == 0:
            self._handle_flat(self.j.execution(ex["id"]))
            return
        self._ensure_closing_stop(self.j.execution(ex["id"]), adapter, pos)

    def _ensure_closing_stop(self, ex: dict[str, Any], adapter: ExchangeAdapter, pos: Position) -> None:
        """Exposure still open while CLOSING must not be left without a stop. If one cannot be
        placed, a durable UNPROTECTED_EXPOSURE incident is raised - requesting an order is never
        reported as coverage."""
        threshold = self.cfg.execution.unknown_resolution_checks
        for o in self.j.orders_for(ex["id"]):
            if o["leg"] == "STOP" and o["status"] in UNRESOLVED_ORDER and o["not_found_checks"] >= threshold:
                self.j.update("orders", "id", o["id"], status="NOT_ACCEPTED", updated_at=self.clock.now())
        orders = self.j.orders_for(ex["id"])
        if any(o["leg"] == "STOP" and o["status"] == "OPEN" and o["qty"] >= pos.qty for o in orders):
            self._mark_stop_verified(ex)
            return
        if any(o["leg"] == "STOP" and o["status"] in UNRESOLVED_ORDER for o in orders):
            self._mark_unprotected(ex, "UNVERIFIED")  # bounded re-check before any replacement
            return
        # an OPEN but undersized stop is not full coverage: mark and add a covering stop below
        plan = ex["exit_plan"]
        spec = ExitSpec.from_dict(ex["exit_spec"]) if ex["exit_spec"] else None
        stop = D(plan["stop"]) if plan else (spec.stop_price if spec else None)
        ref = plan["trigger_reference"] if plan else (str(spec.trigger_reference) if spec else "LAST")
        if stop is None:
            self._mark_unprotected(ex, "NONE")
            self.incident(R.UNPROTECTED_EXPOSURE, ex["account_id"], ex["id"],
                          "close not yet complete and no stop level is available")
            return
        order = self._create_order(ex, "STOP", 0, int(plan["version"]) if plan else 0,
                                   OrderKind.STOP_MARKET, Direction(ex["direction"]).exit_side, pos.qty,
                                   trigger_price=stop, trigger_reference=ref, reduce_only=True)
        outcome, _ = self._send(order, adapter)
        if outcome == "ACK" and self.j.order(order["id"])["status"] == "OPEN":
            self._mark_stop_verified(self.j.execution(ex["id"]))
            return
        self._mark_unprotected(self.j.execution(ex["id"]), "NONE" if outcome == "REJECTED" else "UNVERIFIED")
        self.incident(R.UNPROTECTED_EXPOSURE, ex["account_id"], ex["id"],
                      f"close incomplete and stop placement {outcome}; exposure NOT stop-protected; "
                      "close retried next cycle")

    def _sync_any(self, order: dict[str, Any], adapter: ExchangeAdapter) -> None:
        """Query an order regardless of local status and journal whatever the venue reports."""
        if order["status"] in ("FILLED", "REJECTED"):
            return
        try:
            rep = adapter.get_order(order["native_symbol"], order["client_order_id"])
        except AdapterError:
            return
        if rep is not None:
            self._apply_report(order, rep)

    def _handle_flat(self, ex: dict[str, Any]) -> None:
        """Flatness confirmed by the venue: cancel orphaned exits, then book the result."""
        adapter = self._adapter(ex)
        for o in self.j.orders_for(ex["id"]):  # final sync: no fill may be missed in the books
            self._sync_any(o, adapter)
        for o in self.j.orders_for(ex["id"]):
            if o["leg"] in ("STOP", "TP") and o["status"] == "OPEN":
                if not self._cancel(o, adapter):
                    self.alert("ERROR", "ORPHAN_CANCEL_FAILED", f"could not cancel {o['client_order_id']}",
                               ex["account_id"], ex["trade_id"])
                    return
        self._cancel_strays(ex, adapter)
        crashpoint("before_close_accounting")
        rules = self._rules(ex)
        direction = Direction(ex["direction"])
        exits = self.j.fills_for(ex["id"], EXIT_LEGS)
        avg = ex["avg_fill"]
        gross = sum(((f["price"] - avg) * f["qty"] * rules.contract_multiplier * direction.sign
                     for f in exits), ZERO) if avg is not None else ZERO
        exit_fees = sum((f["fee"] for f in exits), ZERO)
        entry_fees = sum((f["fee"] for f in self.j.fills_for(ex["id"], ("ENTRY",))), ZERO)
        # The recorded reason reflects what actually flattened the position: a requested
        # close that was overtaken by exchange-held exits is reported as that exit.
        reason = ex["close_reason"]
        if exits and not any(f["leg"] == "CLOSE" for f in exits):
            derived = {"STOP": "STOP_LOSS", "TP": "TAKE_PROFIT"}[exits[-1]["leg"]]
            if reason and reason != derived:
                self.j.log_execution(ex["id"], self.clock.now(), "CLOSE_OVERTAKEN",
                                     requested=reason, actual=derived)
            reason = derived
        exit_qty = sum((f["qty"] for f in exits), ZERO)
        if exit_qty != ex["filled_qty"]:
            self.alert("WARNING", "EXIT_QTY_MISMATCH",
                       f"exit fills {exit_qty} != entry fills {ex['filled_qty']}", ex["account_id"],
                       ex["trade_id"])
        now = self.clock.now()
        final: dict[str, Any] = {}
        if ex["exit_plan"]:  # durable per-leg executed/remaining, final at close
            final["exit_plan"] = dict(ex["exit_plan"], legs=self._leg_progress(ex, ex["exit_plan"]))
        since = parse_ts(ex["unprotected_since"])
        if since is not None:  # exposure ended (flat) while not stop-protected: record interval
            dur = Decimal(str(round((now - since).total_seconds(), 3)))
            final["max_unprotected_seconds"] = max(ex["max_unprotected_seconds"] or ZERO, dur)
            self.j.log_execution(ex["id"], now, "UNPROTECTED_INTERVAL", seconds=s(dur), ended_by="FLAT")
        self.j.update_execution(ex["id"], now, **final)
        self.j.update_execution(ex["id"], now, state=str(ExecState.CLOSED), closed_at=now,
                                close_reason=reason or "FLAT", exit_fees=exit_fees, realized_pnl=gross,
                                entry_fees=entry_fees, net_pnl=gross - exit_fees - entry_fees,
                                protected_qty=ZERO, reserved_amount=ZERO, stop_coverage="NONE",
                                tp_coverage="NONE", unprotected_since=None)
        self.j.log_execution(ex["id"], now, "CLOSED", reason=reason, realized=s(gross))
        self._resolve_exec_incidents(ex, "exposure flat confirmed by venue")
        self.alert("INFO", "TRADE_CLOSED", f"{ex['trade_id']} closed ({reason}); gross {gross:.4f}",
                   ex["account_id"], ex["trade_id"])

    # ------------------------------------------------------------ management loop
    MAX_ACCOUNT_ERRORS_PER_CYCLE = 3

    def manage_all(self) -> None:
        """One bounded pass per execution per cycle. An account that keeps failing is capped at
        MAX_ACCOUNT_ERRORS_PER_CYCLE adapter errors per cycle, so it cannot starve the others."""
        for ex in self.j.active_executions(TERMINAL):
            aid = ex["account_id"]
            if self._acct_errors.get(aid, 0) >= self.MAX_ACCOUNT_ERRORS_PER_CYCLE:
                continue
            try:
                self.manage(ex)
            except AdapterError as e:
                self._acct_errors[aid] = self._acct_errors.get(aid, 0) + 1
                self.alert("ERROR", "ADAPTER_ERROR", f"execution {ex['id']}: {e}", ex["account_id"],
                           ex["trade_id"])
            except sqlite3.Error as e:  # this execution's local write failed; others continue
                log.error("journal write failed for execution %s: %s", ex["id"], e)
                self.metrics["cycle_errors"] += 1
            except Exception as e:  # isolate: other accounts keep being managed
                log.exception("managing execution %s failed", ex["id"])
                self.incident(R.INTERNAL_ERROR, ex["account_id"], ex["id"], f"{e!r}")

    def manage(self, ex: dict[str, Any]) -> None:
        st = ExecState(ex["state"])
        now = self.clock.now()
        if st in PRE_FILL:
            self._reconcile_entry(ex)
            ex = self.j.execution(ex["id"])
            st = ExecState(ex["state"])
        if st in OPEN_STATES:
            if ex["exit_plan"] is None:
                self._finalize_entry(ex)
                ex = self.j.execution(ex["id"])
                st = ExecState(ex["state"])
                if st not in OPEN_STATES:
                    return
            if ex["close_requested"]:
                self._begin_close(ex, ex["close_requested"])
                return
            deadline = parse_ts(ex["deadline_at"])
            if deadline is not None and now >= deadline:
                late = (now - deadline).total_seconds()
                if late > self.cfg.execution.time_exit_overdue_grace_seconds:
                    self.alert("WARNING", "TIME_EXIT_OVERDUE",
                               f"holding deadline passed {late:.0f}s ago (best effort after outage)",
                               ex["account_id"], ex["trade_id"])
                self._begin_close(ex, "TIME_EXIT")
                return
            self._ensure_protection(ex)
        elif st is ExecState.CLOSING:
            self._progress_close(ex)

    # ------------------------------------------------------------ event effects per execution
    def _close_execution(self, ex: dict[str, Any], ev: Event) -> bool:
        st = ExecState(ex["state"])
        reason = "LAB_CLOSE"
        if st in TERMINAL or st is ExecState.CLOSING:
            self.j.log_execution(ex["id"], self.clock.now(), "CLOSE_NOOP", event_id=ev.event_id)
            return False
        if st in PRE_FILL:
            self.j.update_execution(ex["id"], self.clock.now(), close_requested=reason, cancel_requested=1)
            self.manage(self.j.execution(ex["id"]))
            return True
        self._begin_close(ex, reason)
        return True

    def _cancel_execution(self, ex: dict[str, Any], ev: Event) -> bool:
        st = ExecState(ex["state"])
        if st is ExecState.RESERVED:
            self._release(ex, ExecState.ENTRY_CANCELLED, reason_code=str(R.ENTRY_CANCELLED),
                          unused_allocation=ex["requested_margin"])
            return True
        if st in PRE_FILL:
            self.j.update_execution(ex["id"], self.clock.now(), cancel_requested=1)
            self.manage(self.j.execution(ex["id"]))  # discovered fills are protected and managed
            return True
        self.j.log_execution(ex["id"], self.clock.now(), "CANCEL_ENTRY_NOOP", event_id=ev.event_id,
                             note="entry already resolved; existing exposure stays managed")
        return False

    def _amend_execution(self, ex: dict[str, Any], ev: Event) -> None:
        assert ev.amend is not None
        st = ExecState(ex["state"])
        now = self.clock.now()
        if st in TERMINAL or st is ExecState.CLOSING:
            self.j.log_execution(ex["id"], now, "AMEND_NOOP", event_id=ev.event_id, state=str(st))
            return
        direction = Direction(ex["direction"])
        rules = self._rules(ex)
        fields: dict[str, Any] = {}
        if ev.amend.holding is not None:
            fields["holding"] = ev.amend.holding.to_dict()
            first = parse_ts(ex["first_fill_at"])
            fields["deadline_at"] = ev.amend.holding.deadline(first)
        spec = ev.amend.exits
        if spec is not None:
            try:
                from .contract import check_exit_order
                check_exit_order(direction, spec, R.AMEND_INVALID_GEOMETRY)
                if not levels_are_tick_aligned(rules.tick_size, spec.stop_price,  # type: ignore[arg-type]
                                               *[t.price for t in spec.take_profits]):  # type: ignore[misc]
                    raise Rejection(R.LEVEL_NOT_TICK_ALIGNED, "amended levels not tick-aligned")
                caps = self._adapter(ex).capabilities()
                if spec.trigger_reference not in caps.trigger_references:
                    raise Rejection(R.TRIGGER_REFERENCE_UNSUPPORTED, str(spec.trigger_reference))
                if st in OPEN_STATES:
                    pos = self._adapter(ex).position(ex["native_symbol"])
                    old = ex["exit_plan"] or {"version": 0}
                    plan = self._build_plan(ex, spec, pos.qty, ex["avg_fill"], int(old["version"]) + 1,
                                            post_fill=False)
                    plan["basis_qty"] = s(pos.qty)
                    plan["amended_by"] = ev.event_id
                    self.j.update_execution(ex["id"], now, exit_spec=spec.to_dict(), **fields)
                    self._store_plan(self.j.execution(ex["id"]), plan)
                    self.j.log_execution(ex["id"], now, "AMEND_APPLIED", event_id=ev.event_id,
                                         previous=old, new=plan)
                    self._ensure_protection(self.j.execution(ex["id"]))
                    return
                fields["exit_spec"] = spec.to_dict()  # pre-fill: used when the entry resolves
            except Rejection as r:
                code = R.AMEND_ALLOCATION_INFEASIBLE if r.code is R.TP_ALLOCATION_INFEASIBLE else (
                    R.AMEND_INVALID_GEOMETRY if r.code is R.POST_FILL_GEOMETRY_INVALID else r.code)
                self.j.log_execution(ex["id"], now, "AMEND_REJECTED", event_id=ev.event_id,
                                     code=str(code), message=r.message)
                self.alert("WARNING", str(code), f"amendment rejected for {ex['account_id']}: {r.message}"
                           " (previous exit plan stays active)", ex["account_id"], ex["trade_id"])
                return
        if fields:
            self.j.update_execution(ex["id"], now, **fields)
            self.j.log_execution(ex["id"], now, "AMEND_APPLIED", event_id=ev.event_id,
                                 fields={k: (v if not isinstance(v, datetime) else iso(v))
                                         for k, v in fields.items()})
