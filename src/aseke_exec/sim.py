"""Deterministic, persistent simulated exchange (SYNTHETIC - not a model of any real venue).

State lives in its own SQLite file, independent of the executor journal, so a
worker restart never erases simulated orders/positions and the test harness can
advance the market (and trigger exchange-held exits) while the worker is down.

Synthetic assumptions (documented in docs/architecture.md):
* one-way position mode, isolated margin only for positions opened by the engine;
* IOC limit entries match the scripted order book level by level (partial fills
  are natural when depth inside the limit is thin) and consume liquidity;
* trigger (stop / take-profit) orders are reduce-only market orders evaluated
  against their declared reference price (LAST/MARK/INDEX) whenever the market is
  updated; stops are evaluated before take-profits in the same tick; if the book
  is exhausted the remainder fills at the last level price;
* fees: flat taker fee rate per venue on every fill; no maker rebates;
* NO liquidation engine and NO funding payments are modelled.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .adapter import (
    AdapterRejected, AdapterTimeout, AdapterUnavailable, Balance, Capabilities, ExchangeAdapter,
    FillReport, OrderBook, OrderKind, OrderReport, OrderRequest, OrderStatus, Position,
    ReferencePrices,
)
from .clock import Clock, iso, parse_utc
from .config import SimConfig
from .domain import Direction, InstrumentRules, MarginMode, PriceRef, Side
from .num import ZERO, D, ceil_to_step, floor_to_step, is_multiple, quantize_fee, s

FAULT_OPS = ("place_entry", "place_protective", "place_stop", "place_tp", "place_close", "amend",
             "cancel", "get_order", "set_margin", "quote", "trigger")
FAULT_MODES = ("REJECT", "TIMEOUT_BEFORE_ACCEPT", "TIMEOUT_AFTER_ACCEPT", "NO_FILL", "PARTIAL",
               "SHIFT_BOOK", "STALE_NOT_FOUND", "DUPLICATE_FILLS")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS venues (venue TEXT PRIMARY KEY, taker_fee_rate TEXT NOT NULL,
  supports_isolated INTEGER NOT NULL, supports_cross INTEGER NOT NULL,
  supports_amend INTEGER NOT NULL, trigger_refs TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS instruments (venue TEXT NOT NULL, symbol TEXT NOT NULL,
  rules TEXT NOT NULL, PRIMARY KEY (venue, symbol));
CREATE TABLE IF NOT EXISTS markets (venue TEXT NOT NULL, symbol TEXT NOT NULL,
  last TEXT NOT NULL, mark TEXT NOT NULL, idx TEXT NOT NULL, bids TEXT NOT NULL,
  asks TEXT NOT NULL, quote_lag_seconds INTEGER NOT NULL DEFAULT 0, updated_at TEXT,
  PRIMARY KEY (venue, symbol));
CREATE TABLE IF NOT EXISTS accounts (venue TEXT NOT NULL, account TEXT NOT NULL,
  currency TEXT NOT NULL, wallet TEXT NOT NULL, PRIMARY KEY (venue, account));
CREATE TABLE IF NOT EXISTS margin_settings (venue TEXT NOT NULL, account TEXT NOT NULL,
  symbol TEXT NOT NULL, mode TEXT NOT NULL, leverage INTEGER NOT NULL,
  PRIMARY KEY (venue, account, symbol));
CREATE TABLE IF NOT EXISTS positions (venue TEXT NOT NULL, account TEXT NOT NULL,
  symbol TEXT NOT NULL, side TEXT, qty TEXT NOT NULL, avg_price TEXT, margin TEXT NOT NULL,
  leverage INTEGER, mode TEXT, PRIMARY KEY (venue, account, symbol));
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, venue TEXT NOT NULL,
  account TEXT NOT NULL, client_order_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
  kind TEXT NOT NULL, qty TEXT NOT NULL, limit_price TEXT, trigger_price TEXT, trigger_ref TEXT,
  reduce_only INTEGER NOT NULL, status TEXT NOT NULL, filled_qty TEXT NOT NULL,
  avg_price TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (venue, account, client_order_id));
CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY AUTOINCREMENT, venue TEXT NOT NULL,
  account TEXT NOT NULL, order_id INTEGER NOT NULL, client_order_id TEXT NOT NULL,
  symbol TEXT NOT NULL, side TEXT NOT NULL, qty TEXT NOT NULL, price TEXT NOT NULL,
  fee TEXT NOT NULL, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS faults (id INTEGER PRIMARY KEY AUTOINCREMENT, venue TEXT NOT NULL,
  account TEXT, op TEXT NOT NULL, mode TEXT NOT NULL, param TEXT, remaining INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, venue TEXT NOT NULL,
  account TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL, amount TEXT NOT NULL, ref TEXT);
"""


class SimReject(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class SimTimeout(Exception):
    pass


def _levels(js: str) -> list[list[Decimal]]:
    return [[D(p), D(q)] for p, q in json.loads(js)]


def _levels_js(levels: list[list[Decimal]] | list[tuple[Decimal, Decimal]]) -> str:
    return json.dumps([[s(D(p)), s(D(q))] for p, q in levels if D(q) > 0])


class SimExchange:
    def __init__(self, path: str | Path, clock: Clock):
        # Writable simulator store: only on the approved runtime, also for library use
        # (runtime_policy; no bypass). Checked before any directory or file is created.
        from .runtime_policy import require_operational
        require_operational("opening the simulator database for writing")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        # Rollback-journal (DELETE) mode, not WAL: the simulator is written by the worker AND by
        # the operator/test harness (`sim set-market`, fault arming) from other processes. With
        # the embedded SQLite affected by the WAL-reset bug (< 3.51.3 / 3.50.7 / 3.44.6), WAL
        # with multiple writer connections is avoided. Busy waits are bounded by `timeout`.
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def _now(self) -> str:
        return iso(self.clock.now())  # type: ignore[return-value]

    # ------------------------------------------------------------ setup/harness
    def install(self, cfg: SimConfig, reset: bool = False) -> None:
        """Idempotently create venues, instruments, accounts and a default market."""
        with self.tx() as c:
            if reset:
                for t in ("venues", "instruments", "markets", "accounts", "margin_settings",
                          "positions", "orders", "fills", "faults", "ledger"):
                    c.execute(f"DELETE FROM {t}")
            for v in cfg.venues.values():
                c.execute("INSERT OR IGNORE INTO venues VALUES (?,?,?,?,?,?)",
                          (v.name, s(v.taker_fee_rate), int(v.supports_isolated),
                           int(v.supports_cross), int(v.supports_amend),
                           json.dumps(sorted(str(r) for r in v.trigger_references))))
                for sym, rules in v.instruments.items():
                    c.execute("INSERT OR IGNORE INTO instruments VALUES (?,?,?)",
                              (v.name, sym, json.dumps(rules.to_dict())))
            for a in cfg.accounts.values():
                c.execute("INSERT OR IGNORE INTO accounts VALUES (?,?,?,?)",
                          (a.venue, a.id, a.currency, s(a.starting_balance)))
        for v in cfg.venues.values():
            for sym in v.instruments:
                if self._market_row(v.name, sym) is None:
                    self.set_simple_market(v.name, sym, cfg.initial_price,
                                           depth_btc=cfg.initial_depth_btc)

    def instrument(self, venue: str, symbol: str) -> InstrumentRules:
        r = self.conn.execute("SELECT rules FROM instruments WHERE venue=? AND symbol=?",
                              (venue, symbol)).fetchone()
        if r is None:
            raise SimReject("UNKNOWN_SYMBOL", f"{venue}:{symbol}")
        return InstrumentRules.from_dict(json.loads(r["rules"]))

    def symbols(self, venue: str) -> list[str]:
        return [r["symbol"] for r in self.conn.execute(
            "SELECT symbol FROM instruments WHERE venue=? ORDER BY symbol", (venue,))]

    def venue_row(self, venue: str) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM venues WHERE venue=?", (venue,)).fetchone()
        if r is None:
            raise SimReject("UNKNOWN_VENUE", venue)
        return r

    def _market_row(self, venue: str, symbol: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM markets WHERE venue=? AND symbol=?",
                                 (venue, symbol)).fetchone()

    def set_market(self, venue: str, symbol: str, *, last: Any = None, mark: Any = None,
                   index: Any = None, bids: Any = None, asks: Any = None,
                   quote_lag_seconds: int | None = None, process: bool = True) -> None:
        row = self._market_row(venue, symbol)
        cur = dict(row) if row else {"last": "0", "mark": "0", "idx": "0", "bids": "[]",
                                     "asks": "[]", "quote_lag_seconds": 0}
        new = {
            "last": s(D(last)) if last is not None else cur["last"],
            "mark": s(D(mark)) if mark is not None else cur["mark"],
            "idx": s(D(index)) if index is not None else cur["idx"],
            "bids": _levels_js(sorted(((D(p), D(q)) for p, q in bids), key=lambda x: -x[0]))
            if bids is not None else cur["bids"],
            "asks": _levels_js(sorted(((D(p), D(q)) for p, q in asks), key=lambda x: x[0]))
            if asks is not None else cur["asks"],
            "lag": quote_lag_seconds if quote_lag_seconds is not None else cur["quote_lag_seconds"],
        }
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?,?,?)",
                      (venue, symbol, new["last"], new["mark"], new["idx"], new["bids"],
                       new["asks"], new["lag"], self._now()))
        if process:
            self.process_triggers(venue, symbol)

    def set_simple_market(self, venue: str, symbol: str, price: Any, *, depth_btc: Any = "1",
                          levels: int = 5, last: Any = None, mark: Any = None, index: Any = None,
                          process: bool = True) -> None:
        """Book centred on ``price``: best bid/ask one tick away, 5 levels 0.5 apart,
        ``depth_btc`` per level (converted to native contracts)."""
        rules = self.instrument(venue, symbol)
        p = D(price)
        tick = rules.tick_size
        gap = max(tick, ceil_to_step(Decimal("0.5"), tick))
        qty = floor_to_step(D(depth_btc) / rules.contract_multiplier, rules.qty_step)
        bb = floor_to_step(p - tick, tick)
        ba = ceil_to_step(p + tick, tick)
        bids = [(bb - gap * i, qty) for i in range(levels)]
        asks = [(ba + gap * i, qty) for i in range(levels)]
        self.set_market(venue, symbol, last=last if last is not None else p,
                        mark=mark if mark is not None else p,
                        index=index if index is not None else p, bids=bids, asks=asks,
                        process=process)

    def add_fault(self, venue: str, op: str, mode: str, *, account: str | None = None,
                  param: str | None = None, count: int = 1) -> None:
        if op not in FAULT_OPS or mode.split(":")[0] not in FAULT_MODES:
            raise ValueError(f"unknown fault {op}/{mode}")
        with self.tx() as c:
            c.execute("INSERT INTO faults (venue, account, op, mode, param, remaining) "
                      "VALUES (?,?,?,?,?,?)", (venue, account, op, mode, param, count))

    def set_wallet(self, venue: str, account: str, amount: Any) -> None:
        with self.tx() as c:
            c.execute("UPDATE accounts SET wallet=? WHERE venue=? AND account=?",
                      (s(D(amount)), venue, account))

    def _take_fault(self, venue: str, account: str, op: str) -> sqlite3.Row | None:
        with self.tx() as c:
            r = c.execute("SELECT * FROM faults WHERE venue=? AND (account IS NULL OR account=?) "
                          "AND op=? AND remaining>0 ORDER BY id LIMIT 1",
                          (venue, account, op)).fetchone()
            if r is not None:
                c.execute("UPDATE faults SET remaining=remaining-1 WHERE id=?", (r["id"],))
        return r

    # ------------------------------------------------------------------ reads
    def quote(self, venue: str, symbol: str) -> dict[str, Any]:
        f = self._take_fault(venue, None, "quote")
        if f is not None and f["mode"].startswith("TIMEOUT"):
            raise SimTimeout()
        m = self._market_row(venue, symbol)
        if m is None:
            raise SimReject("NO_MARKET", symbol)
        ts = self.clock.now() - timedelta(seconds=int(m["quote_lag_seconds"]))
        return {"bids": _levels(m["bids"]), "asks": _levels(m["asks"]), "last": D(m["last"]),
                "mark": D(m["mark"]), "index": D(m["idx"]), "ts": ts}

    def _pos(self, c: sqlite3.Connection, venue: str, account: str, symbol: str) -> dict[str, Any]:
        r = c.execute("SELECT * FROM positions WHERE venue=? AND account=? AND symbol=?",
                      (venue, account, symbol)).fetchone()
        if r is None:
            return {"side": None, "qty": ZERO, "avg_price": None, "margin": ZERO,
                    "leverage": None, "mode": None}
        return {"side": r["side"], "qty": D(r["qty"]), "avg_price": D(r["avg_price"]) if r["avg_price"] else None,
                "margin": D(r["margin"]), "leverage": r["leverage"], "mode": r["mode"]}

    def position(self, venue: str, account: str, symbol: str) -> dict[str, Any]:
        return self._pos(self.conn, venue, account, symbol)

    def positions(self, venue: str, account: str) -> list[tuple[str, dict[str, Any]]]:
        rows = self.conn.execute("SELECT symbol FROM positions WHERE venue=? AND account=?",
                                 (venue, account)).fetchall()
        return [(r["symbol"], self.position(venue, account, r["symbol"])) for r in rows]

    def balance(self, venue: str, account: str) -> dict[str, Any]:
        a = self.conn.execute("SELECT * FROM accounts WHERE venue=? AND account=?",
                              (venue, account)).fetchone()
        if a is None:
            raise SimReject("UNKNOWN_ACCOUNT", account)
        used = sum((D(r["margin"]) for r in self.conn.execute(
            "SELECT margin FROM positions WHERE venue=? AND account=?", (venue, account))), ZERO)
        wallet = D(a["wallet"])
        return {"currency": a["currency"], "wallet": wallet, "available": wallet - used}

    def order(self, venue: str, account: str, cid: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM orders WHERE venue=? AND account=? AND client_order_id=?",
                              (venue, account, cid)).fetchone()
        if r is None:
            return None
        o = dict(r)
        o["fills"] = [dict(f) for f in self.conn.execute(
            "SELECT * FROM fills WHERE order_id=? ORDER BY id", (r["id"],))]
        return o

    def get_order(self, venue: str, account: str, cid: str) -> dict[str, Any] | None:
        f = self._take_fault(venue, account, "get_order")
        mode = f["mode"] if f else None
        if mode == "TIMEOUT_BEFORE_ACCEPT":
            raise SimTimeout()
        if mode == "STALE_NOT_FOUND":
            return None
        o = self.order(venue, account, cid)
        if o is not None and mode == "DUPLICATE_FILLS":
            o["fills"] = o["fills"] + o["fills"]
        return o

    def open_orders(self, venue: str, account: str, symbol: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT client_order_id FROM orders WHERE venue=? AND account=? "
                                 "AND symbol=? AND status='OPEN' ORDER BY id",
                                 (venue, account, symbol)).fetchall()
        return [self.order(venue, account, r["client_order_id"]) for r in rows]  # type: ignore[misc]

    def all_orders(self, venue: str, account: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM orders WHERE venue=? AND account=? ORDER BY id",
                                 (venue, account)).fetchall()

    # ------------------------------------------------------------------ writes
    def set_margin(self, venue: str, account: str, symbol: str, mode: str, leverage: int) -> None:
        f = self._take_fault(venue, account, "set_margin")
        if f and f["mode"] == "REJECT":
            raise SimReject(f["param"] or "SIM_FAULT_REJECT", "injected")
        if f and f["mode"].startswith("TIMEOUT"):
            raise SimTimeout()
        v = self.venue_row(venue)
        rules = self.instrument(venue, symbol)
        if mode == "ISOLATED" and not v["supports_isolated"]:
            raise SimReject("ISOLATED_UNSUPPORTED", f"{venue} supports cross margin only")
        if mode == "CROSS" and not v["supports_cross"]:
            raise SimReject("CROSS_UNSUPPORTED", venue)
        if not rules.min_leverage <= leverage <= rules.max_leverage:
            raise SimReject("LEVERAGE_OUT_OF_RANGE",
                            f"{leverage} not in [{rules.min_leverage}, {rules.max_leverage}]")
        with self.tx() as c:
            p = self._pos(c, venue, account, symbol)
            if p["qty"] > 0 and (p["leverage"] != leverage or p["mode"] != mode):
                raise SimReject("POSITION_OPEN", "cannot change margin settings with an open position")
            c.execute("INSERT OR REPLACE INTO margin_settings VALUES (?,?,?,?,?)",
                      (venue, account, symbol, mode, leverage))

    def place_order(self, venue: str, account: str, req: dict[str, Any]) -> dict[str, Any]:
        kind = req["kind"]
        op = {"ENTRY_LIMIT_IOC": "place_entry", "CLOSE_MARKET": "place_close"}.get(kind, "place_protective")
        f = None
        if op == "place_protective":  # kind-specific fault first, then the generic one
            f = self._take_fault(venue, account, "place_stop" if kind == "STOP_MARKET" else "place_tp")
        f = f or self._take_fault(venue, account, op)
        mode = f["mode"] if f else None
        if mode == "REJECT":
            raise SimReject(f["param"] or "SIM_FAULT_REJECT", "injected rejection")  # type: ignore[index]
        if mode == "TIMEOUT_BEFORE_ACCEPT":
            raise SimTimeout()
        with self.tx() as c:
            result = self._place(c, venue, account, req, mode, f["param"] if f else None)
        if mode == "TIMEOUT_AFTER_ACCEPT":
            raise SimTimeout()
        return self.order(venue, account, req["client_order_id"]) or result

    def _place(self, c: sqlite3.Connection, venue: str, account: str, req: dict[str, Any],
               fault: str | None, param: str | None) -> dict[str, Any]:
        rules = self.instrument(venue, req["symbol"])
        v = self.venue_row(venue)
        cid = req["client_order_id"]
        if c.execute("SELECT 1 FROM orders WHERE venue=? AND account=? AND client_order_id=?",
                     (venue, account, cid)).fetchone():
            raise SimReject("DUPLICATE_CLIENT_ORDER_ID", cid)
        qty = D(req["qty"])
        if qty <= 0 or not is_multiple(qty, rules.qty_step) or qty < rules.min_qty:
            raise SimReject("INVALID_QTY", f"{qty} (step {rules.qty_step}, min {rules.min_qty})")
        side = req["side"]
        kind = req["kind"]
        sym = req["symbol"]
        pos = self._pos(c, venue, account, sym)
        pos_side_order = "SELL" if pos["side"] == "LONG" else "BUY" if pos["side"] == "SHORT" else None
        now = self._now()
        m = self._market_row(venue, sym)
        if m is None:
            raise SimReject("NO_MARKET", sym)
        bids, asks = _levels(m["bids"]), _levels(m["asks"])

        def insert(status: str, limit: Any = None, trig: Any = None, ref: Any = None) -> int:
            cur = c.execute(
                "INSERT INTO orders (venue, account, client_order_id, symbol, side, kind, qty, "
                "limit_price, trigger_price, trigger_ref, reduce_only, status, filled_qty, avg_price,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (venue, account, cid, sym, side, kind, s(qty), s(D(limit)) if limit is not None else None,
                 s(D(trig)) if trig is not None else None, ref, int(bool(req.get("reduce_only"))),
                 status, "0", None, now, now))
            return int(cur.lastrowid)  # type: ignore[arg-type]

        if kind == "ENTRY_LIMIT_IOC":
            if req.get("reduce_only"):
                raise SimReject("INVALID_REQUEST", "entry cannot be reduce-only")
            ms = c.execute("SELECT * FROM margin_settings WHERE venue=? AND account=? AND symbol=?",
                           (venue, account, sym)).fetchone()
            if ms is None:
                raise SimReject("MARGIN_SETTINGS_REQUIRED", "set margin mode/leverage first")
            want = "LONG" if side == "BUY" else "SHORT"
            if pos["qty"] > 0 and pos["side"] != want:
                raise SimReject("OPPOSITE_POSITION", "one-way mode: opposite position open")
            limit = D(req["limit_price"])
            if not is_multiple(limit, rules.tick_size):
                raise SimReject("INVALID_PRICE", f"limit {limit} not on tick {rules.tick_size}")
            lev = int(ms["leverage"])
            est_px = limit if side == "BUY" else (bids[0][0] if bids else limit)
            need = qty * rules.contract_multiplier * est_px / lev
            need += quantize_fee(qty * rules.contract_multiplier * est_px * D(v["taker_fee_rate"]))
            bal = self.balance(venue, account)
            if bal["available"] < need:
                raise SimReject("INSUFFICIENT_MARGIN", f"need {need}, available {bal['available']}")
            if fault == "SHIFT_BOOK":
                delta = D(param or "0")
                bids = [[p + delta, q] for p, q in bids]
                asks = [[p + delta, q] for p, q in asks]
            oid = insert("OPEN", limit=limit)
            target = qty
            if fault == "NO_FILL":
                target = ZERO
            elif fault == "PARTIAL":
                target = floor_to_step(qty * D(param or "0.5"), rules.qty_step)
            book = asks if side == "BUY" else bids
            ok = (lambda p: p <= limit) if side == "BUY" else (lambda p: p >= limit)
            fills = self._walk(book, target, ok, extend=False)
            for px, fq in fills:
                self._fill(c, venue, account, sym, oid, cid, side, fq, px, rules, lev, ms["mode"],
                           D(v["taker_fee_rate"]), reduce=False)
            self._store_book(c, venue, sym, bids if side == "BUY" else book,
                             book if side == "BUY" else asks)
            filled = sum((q for _, q in fills), ZERO)
            status = "FILLED" if filled == qty else ("CANCELLED" if filled > 0 else "EXPIRED")
            c.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (status, now, oid))
        elif kind in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
            if not req.get("reduce_only"):
                raise SimReject("REDUCE_ONLY_REQUIRED", "protective orders must be reduce-only")
            if pos["qty"] <= 0 or side != pos_side_order:
                raise SimReject("REDUCE_ONLY_NO_POSITION", "no position this order could reduce")
            trig = D(req["trigger_price"])
            ref = req.get("trigger_reference") or "LAST"
            if ref not in json.loads(v["trigger_refs"]):
                raise SimReject("TRIGGER_REFERENCE_UNSUPPORTED", ref)
            if not is_multiple(trig, rules.tick_size):
                raise SimReject("INVALID_PRICE", f"trigger {trig} not on tick")
            if self._fires(kind, side, trig, self._ref_price(m, ref)):
                raise SimReject("WOULD_TRIGGER_IMMEDIATELY", f"{kind} {trig} vs {ref}")
            insert("OPEN", trig=trig, ref=ref)
        elif kind == "CLOSE_MARKET":
            if not req.get("reduce_only"):
                raise SimReject("REDUCE_ONLY_REQUIRED", "close must be reduce-only")
            if pos["qty"] <= 0 or side != pos_side_order:
                raise SimReject("REDUCE_ONLY_NO_POSITION", "nothing to close")
            oid = insert("OPEN")
            target = min(qty, pos["qty"])
            if fault == "PARTIAL":
                target = max(rules.qty_step, floor_to_step(target * D(param or "0.5"), rules.qty_step))
            if fault == "NO_FILL":
                target = ZERO
            book = asks if side == "BUY" else bids
            fills = self._walk(book, target, lambda p: True, extend=False)
            for px, fq in fills:
                self._fill(c, venue, account, sym, oid, cid, side, fq, px, rules, pos["leverage"] or 1,
                           pos["mode"], D(v["taker_fee_rate"]), reduce=True)
            self._store_book(c, venue, sym, book if side == "SELL" else bids,
                             book if side == "BUY" else asks)
            filled = sum((q for _, q in fills), ZERO)
            status = "FILLED" if filled == qty else ("CANCELLED" if filled > 0 else "EXPIRED")
            c.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (status, now, oid))
        else:
            raise SimReject("UNSUPPORTED_ORDER_KIND", kind)
        return self._order_c(c, venue, account, cid)

    def _order_c(self, c: sqlite3.Connection, venue: str, account: str, cid: str) -> dict[str, Any]:
        r = c.execute("SELECT * FROM orders WHERE venue=? AND account=? AND client_order_id=?",
                      (venue, account, cid)).fetchone()
        o = dict(r)
        o["fills"] = [dict(f) for f in c.execute("SELECT * FROM fills WHERE order_id=?", (r["id"],))]
        return o

    def _store_book(self, c: sqlite3.Connection, venue: str, sym: str, bids: Any, asks: Any) -> None:
        c.execute("UPDATE markets SET bids=?, asks=? WHERE venue=? AND symbol=?",
                  (_levels_js(bids), _levels_js(asks), venue, sym))

    @staticmethod
    def _walk(book: list[list[Decimal]], qty: Decimal, ok: Any, extend: bool,
              fallback: Decimal | None = None) -> list[tuple[Decimal, Decimal]]:
        out: list[tuple[Decimal, Decimal]] = []
        rem = qty
        for lvl in book:
            if rem <= 0 or not ok(lvl[0]):
                break
            take = min(lvl[1], rem)
            if take > 0:
                out.append((lvl[0], take))
                lvl[1] -= take
                rem -= take
        if rem > 0 and extend:
            px = book[-1][0] if book else fallback
            if px is not None:
                out.append((px, rem))
        return out

    @staticmethod
    def _ref_price(m: sqlite3.Row, ref: str) -> Decimal:
        return D({"LAST": m["last"], "MARK": m["mark"], "INDEX": m["idx"]}[ref])

    @staticmethod
    def _fires(kind: str, side: str, trig: Decimal, ref: Decimal) -> bool:
        if kind == "STOP_MARKET":
            return ref <= trig if side == "SELL" else ref >= trig
        return ref >= trig if side == "SELL" else ref <= trig

    def _fill(self, c: sqlite3.Connection, venue: str, account: str, sym: str, oid: int, cid: str,
              side: str, qty: Decimal, price: Decimal, rules: InstrumentRules, lev: int,
              mode: str | None, fee_rate: Decimal, reduce: bool) -> None:
        now = self._now()
        mult = rules.contract_multiplier
        notional = qty * mult * price
        fee = quantize_fee(notional * fee_rate)
        pos = self._pos(c, venue, account, sym)
        wallet = D(c.execute("SELECT wallet FROM accounts WHERE venue=? AND account=?",
                             (venue, account)).fetchone()["wallet"])
        if not reduce:
            new_side = "LONG" if side == "BUY" else "SHORT"
            q0 = pos["qty"]
            avg = ((pos["avg_price"] or ZERO) * q0 + price * qty) / (q0 + qty)
            margin = pos["margin"] + notional / lev
            c.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?)",
                      (venue, account, sym, new_side, s(q0 + qty), s(avg), s(margin), lev, mode))
        else:
            q0 = pos["qty"]
            assert qty <= q0, "reduce-only fill larger than position"
            sign = 1 if pos["side"] == "LONG" else -1
            realized = (price - pos["avg_price"]) * qty * mult * sign
            release = pos["margin"] * qty / q0
            rem = q0 - qty
            wallet += realized
            c.execute("INSERT INTO ledger (venue, account, ts, kind, amount, ref) VALUES (?,?,?,?,?,?)",
                      (venue, account, now, "REALIZED_PNL", s(realized), cid))
            if rem == 0:
                c.execute("DELETE FROM positions WHERE venue=? AND account=? AND symbol=?",
                          (venue, account, sym))
            else:
                c.execute("UPDATE positions SET qty=?, margin=? WHERE venue=? AND account=? AND symbol=?",
                          (s(rem), s(pos["margin"] - release), venue, account, sym))
        wallet -= fee
        c.execute("UPDATE accounts SET wallet=? WHERE venue=? AND account=?", (s(wallet), venue, account))
        c.execute("INSERT INTO ledger (venue, account, ts, kind, amount, ref) VALUES (?,?,?,?,?,?)",
                  (venue, account, now, "FEE", s(-fee), cid))
        c.execute("INSERT INTO fills (venue, account, order_id, client_order_id, symbol, side, qty, "
                  "price, fee, ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (venue, account, oid, cid, sym, side, s(qty), s(price), s(fee), now))
        o = c.execute("SELECT filled_qty, avg_price FROM orders WHERE id=?", (oid,)).fetchone()
        fq0 = D(o["filled_qty"])
        avg0 = D(o["avg_price"]) if o["avg_price"] else ZERO
        fq1 = fq0 + qty
        c.execute("UPDATE orders SET filled_qty=?, avg_price=?, updated_at=? WHERE id=?",
                  (s(fq1), s((avg0 * fq0 + price * qty) / fq1), now, oid))

    def process_triggers(self, venue: str, symbol: str) -> int:
        """Evaluate working trigger orders against the current reference prices."""
        fired = 0
        with self.tx() as c:
            m = self._market_row(venue, symbol)
            if m is None:
                return 0
            v = self.venue_row(venue)
            rules = self.instrument(venue, symbol)
            bids, asks = _levels(m["bids"]), _levels(m["asks"])
            rows = c.execute(
                "SELECT * FROM orders WHERE venue=? AND symbol=? AND status='OPEN' AND trigger_price "
                "IS NOT NULL ORDER BY CASE kind WHEN 'STOP_MARKET' THEN 0 ELSE 1 END, id",
                (venue, symbol)).fetchall()
            now = self._now()
            for o in rows:
                ref = self._ref_price(m, o["trigger_ref"])
                if not self._fires(o["kind"], o["side"], D(o["trigger_price"]), ref):
                    continue
                pos = self._pos(c, venue, o["account"], symbol)
                closing_side = "SELL" if pos["side"] == "LONG" else "BUY" if pos["side"] == "SHORT" else None
                if pos["qty"] <= 0 or closing_side != o["side"]:
                    c.execute("UPDATE orders SET status='CANCELLED', updated_at=? WHERE id=?", (now, o["id"]))
                    continue
                q = min(D(o["qty"]), pos["qty"])
                f = c.execute("SELECT * FROM faults WHERE venue=? AND (account IS NULL OR account=?) AND "
                              "op='trigger' AND remaining>0 ORDER BY id LIMIT 1",
                              (venue, o["account"])).fetchone()
                status = "FILLED"
                if f is not None and f["mode"] == "PARTIAL":  # triggered exit only partially fills
                    c.execute("UPDATE faults SET remaining=remaining-1 WHERE id=?", (f["id"],))
                    q = max(rules.qty_step, floor_to_step(q * D(f["param"] or "0.5"), rules.qty_step))
                    status = "CANCELLED"  # remainder of the triggered market order cancelled
                book = bids if o["side"] == "SELL" else asks
                for px, fq in self._walk(book, q, lambda p: True, extend=True, fallback=ref):
                    self._fill(c, venue, o["account"], symbol, o["id"], o["client_order_id"],
                               o["side"], fq, px, rules, pos["leverage"] or 1, pos["mode"],
                               D(v["taker_fee_rate"]), reduce=True)
                c.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (status, now, o["id"]))
                fired += 1
            self._store_book(c, venue, symbol, bids, asks)
        return fired

    def amend_order(self, venue: str, account: str, cid: str, qty: Any = None,
                    trigger_price: Any = None) -> dict[str, Any]:
        f = self._take_fault(venue, account, "amend")
        mode = f["mode"] if f else None
        if mode == "REJECT":
            raise SimReject(f["param"] or "SIM_FAULT_REJECT", "injected")  # type: ignore[index]
        if mode == "TIMEOUT_BEFORE_ACCEPT":
            raise SimTimeout()
        v = self.venue_row(venue)
        if not v["supports_amend"]:
            raise SimReject("AMEND_UNSUPPORTED", venue)
        with self.tx() as c:
            o = c.execute("SELECT * FROM orders WHERE venue=? AND account=? AND client_order_id=?",
                          (venue, account, cid)).fetchone()
            if o is None:
                raise SimReject("ORDER_NOT_FOUND", cid)
            if o["status"] != "OPEN":
                raise SimReject("ORDER_NOT_OPEN", o["status"])
            rules = self.instrument(venue, o["symbol"])
            nq = D(qty) if qty is not None else D(o["qty"])
            nt = D(trigger_price) if trigger_price is not None else D(o["trigger_price"])
            if nq < rules.min_qty or not is_multiple(nq, rules.qty_step):
                raise SimReject("INVALID_QTY", str(nq))
            m = self._market_row(venue, o["symbol"])
            if self._fires(o["kind"], o["side"], nt, self._ref_price(m, o["trigger_ref"])):  # type: ignore[arg-type]
                raise SimReject("WOULD_TRIGGER_IMMEDIATELY", str(nt))
            c.execute("UPDATE orders SET qty=?, trigger_price=?, updated_at=? WHERE id=?",
                      (s(nq), s(nt), self._now(), o["id"]))
        if mode == "TIMEOUT_AFTER_ACCEPT":
            raise SimTimeout()
        return self.order(venue, account, cid)  # type: ignore[return-value]

    def cancel_order(self, venue: str, account: str, cid: str) -> dict[str, Any]:
        f = self._take_fault(venue, account, "cancel")
        mode = f["mode"] if f else None
        if mode == "REJECT":
            raise SimReject(f["param"] or "SIM_FAULT_REJECT", "injected")  # type: ignore[index]
        if mode == "TIMEOUT_BEFORE_ACCEPT":
            raise SimTimeout()
        with self.tx() as c:
            o = c.execute("SELECT * FROM orders WHERE venue=? AND account=? AND client_order_id=?",
                          (venue, account, cid)).fetchone()
            if o is None:
                raise SimReject("ORDER_NOT_FOUND", cid)
            if o["status"] != "OPEN":
                raise SimReject("ORDER_NOT_OPEN", o["status"])
            c.execute("UPDATE orders SET status='CANCELLED', updated_at=? WHERE id=?",
                      (self._now(), o["id"]))
        if mode == "TIMEOUT_AFTER_ACCEPT":
            raise SimTimeout()
        return self.order(venue, account, cid)  # type: ignore[return-value]

    # ---------------------------------------------------------- harness extras
    def force_position_order(self, venue: str, account: str, symbol: str, side: str, qty: Any,
                             leverage: int = 5) -> None:
        """Harness-only: create exposure the executor does not know about."""
        rules = self.instrument(venue, symbol)
        self.set_margin(venue, account, symbol, "ISOLATED", leverage)
        q = self.quote(venue, symbol)
        limit = (q["asks"][-1][0] if side == "BUY" else q["bids"][-1][0])
        self.place_order(venue, account, {"client_order_id": f"MANUAL-{self._now()}", "symbol": symbol,
                                          "side": side, "kind": "ENTRY_LIMIT_IOC", "qty": s(D(qty)),
                                          "limit_price": s(floor_to_step(limit, rules.tick_size)),
                                          "reduce_only": False})


# ===================================================================== adapter
def _report(o: dict[str, Any], currency: str) -> OrderReport:
    fills = tuple(FillReport(fill_id=f"{o['venue']}-F{f['id']}", qty=D(f["qty"]), price=D(f["price"]),
                             fee=D(f["fee"]), fee_currency=currency, ts=parse_utc(f["ts"]))
                  for f in o.get("fills", []))
    return OrderReport(
        client_order_id=o["client_order_id"], venue_order_id=f"{o['venue']}-O{o['id']}",
        symbol=o["symbol"], side=Side(o["side"]), kind=OrderKind(o["kind"]),
        status=OrderStatus(o["status"]), qty=D(o["qty"]), filled_qty=D(o["filled_qty"]),
        avg_price=D(o["avg_price"]) if o.get("avg_price") else None,
        trigger_price=D(o["trigger_price"]) if o.get("trigger_price") else None, fills=fills)


class SimAdapter(ExchangeAdapter):
    """ExchangeAdapter over the persistent SimExchange for one synthetic account."""

    def __init__(self, exchange: SimExchange, venue: str, account_id: str):
        self.ex = exchange
        self.venue = venue
        self.account_id = account_id

    def _currency(self) -> str:
        return self.ex.balance(self.venue, self.account_id)["currency"]

    def _wrap(self, fn: Any, *a: Any, **k: Any) -> Any:
        try:
            return fn(*a, **k)
        except SimReject as e:
            raise AdapterRejected(e.code, e.message) from None
        except SimTimeout:
            raise AdapterTimeout("TIMEOUT", "simulated timeout - outcome unknown") from None
        except Exception as e:  # sqlite busy, etc.
            if isinstance(e, (AdapterRejected, AdapterTimeout)):
                raise
            raise AdapterUnavailable("SIM_ERROR", str(e)) from e

    def capabilities(self) -> Capabilities:
        v = self.ex.venue_row(self.venue)
        refs = frozenset(PriceRef(r) for r in json.loads(v["trigger_refs"]))
        return Capabilities(venue=self.venue, simulated=True,
                            isolated_margin=bool(v["supports_isolated"]),
                            cross_margin=bool(v["supports_cross"]), trigger_references=refs,
                            reduce_only=True, amend_protective=bool(v["supports_amend"]),
                            ioc_limit=True, client_order_ids=True,
                            taker_fee_rate=D(v["taker_fee_rate"]), attached_protection=False)

    def instrument_rules(self, symbol: str) -> InstrumentRules:
        return self._wrap(self.ex.instrument, self.venue, symbol)

    def order_book(self, symbol: str) -> OrderBook:
        q = self._wrap(self.ex.quote, self.venue, symbol)
        return OrderBook(symbol, tuple((p, x) for p, x in q["bids"]),
                         tuple((p, x) for p, x in q["asks"]), q["ts"])

    def reference_prices(self, symbol: str) -> ReferencePrices:
        q = self._wrap(self.ex.quote, self.venue, symbol)
        return ReferencePrices(q["last"], q["mark"], q["index"], q["ts"])

    def balance(self) -> Balance:
        b = self._wrap(self.ex.balance, self.venue, self.account_id)
        return Balance(b["currency"], b["wallet"], b["available"])

    def _position(self, symbol: str, p: dict[str, Any]) -> Position:
        return Position(symbol, Direction(p["side"]) if p["side"] else None, p["qty"],
                        p["avg_price"], p["margin"], p["leverage"],
                        MarginMode(p["mode"]) if p["mode"] else None)

    def position(self, symbol: str) -> Position:
        return self._position(symbol, self._wrap(self.ex.position, self.venue, self.account_id, symbol))

    def positions(self) -> list[Position]:
        return [self._position(sym, p) for sym, p in
                self._wrap(self.ex.positions, self.venue, self.account_id) if p["qty"] > 0]

    def set_margin(self, symbol: str, mode: MarginMode, leverage: int) -> None:
        self._wrap(self.ex.set_margin, self.venue, self.account_id, symbol, str(mode), leverage)

    def place_order(self, req: OrderRequest) -> OrderReport:
        o = self._wrap(self.ex.place_order, self.venue, self.account_id, {
            "client_order_id": req.client_order_id, "symbol": req.symbol, "side": str(req.side),
            "kind": str(req.kind), "qty": s(req.qty), "limit_price": s(req.limit_price),
            "trigger_price": s(req.trigger_price),
            "trigger_reference": str(req.trigger_reference) if req.trigger_reference else None,
            "reduce_only": req.reduce_only})
        return _report(o, self._currency())

    def get_order(self, symbol: str, client_order_id: str) -> OrderReport | None:
        o = self._wrap(self.ex.get_order, self.venue, self.account_id, client_order_id)
        return _report(o, self._currency()) if o else None

    def open_orders(self, symbol: str) -> list[OrderReport]:
        cur = self._currency()
        return [_report(o, cur) for o in self._wrap(self.ex.open_orders, self.venue,
                                                    self.account_id, symbol)]

    def amend_order(self, symbol: str, client_order_id: str, *, qty: Decimal | None = None,
                    trigger_price: Decimal | None = None) -> OrderReport:
        o = self._wrap(self.ex.amend_order, self.venue, self.account_id, client_order_id,
                       s(qty), s(trigger_price))
        return _report(o, self._currency())

    def cancel_order(self, symbol: str, client_order_id: str) -> OrderReport:
        o = self._wrap(self.ex.cancel_order, self.venue, self.account_id, client_order_id)
        return _report(o, self._currency())
