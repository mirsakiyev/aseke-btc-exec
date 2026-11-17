-- Journal schema v1 as shipped in commit 9b4501f (Phase 1); used to test additive migration.
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
