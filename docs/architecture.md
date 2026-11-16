# Architecture, lifecycle and state model (Phase 1)

## Components

```
 Lab (future) / dummy publisher
        │  atomic temp-write + fsync + no-clobber rename   (inbox.publish_event)
        ▼
 var/inbox/*.json ──scan──► Engine.ingest_text ──► journal.events (validated, hashed)
        │                         │ (only after commit)
        └──────────► var/archive | var/quarantine (+ .reason.json)
                                  ▼
                       Engine.process_events  (ordering policy, routing snapshot)
                                  ▼
                  per-account executions  ◄──── Engine.manage_all (every cycle)
                                  ▼
                     ExchangeAdapter (capability-aware) ──► SimAdapter ──► SimExchange
                                                                       (own SQLite file)
```

| Module | Responsibility |
|---|---|
| `num.py` | Decimal-only helpers; floats rejected |
| `contract.py` | Strict v1 parser: types, enums, UTC, geometry, fractions, payload-per-type |
| `planning.py` | Pure math: bounds + tick rounding, sizing, TP allocation, entry-relative levels, geometry, risk |
| `config.py` | TOML (Decimal) config; refuses live mode, floats, cross fallback, non-synthetic names |
| `adapter.py` | Adapter interface, capabilities, structured errors (`Rejected`, `Timeout` = unknown, `Unavailable`) |
| `sim.py` | Persistent deterministic exchange + `SimAdapter` |
| `journal.py` | Executor-owned SQLite ledger (WAL, `synchronous=FULL`, `BEGIN IMMEDIATE`, unique keys) |
| `engine.py` | Lifecycle, ordering, reservation, protection desired-state, closing, recovery, incidents |
| `runtime.py` / `cli.py` | Wiring, worker loop, status, operator commands |
| `scenarios.py` | Deterministic acceptance scenarios (also run by pytest) |

## Execution states (per account execution)

```
REJECTED / SKIPPED            (preflight; terminal, machine-readable reason)
RESERVED ─► ENTRY_SUBMITTED ─► (ack) ─► OPEN_UNPROTECTED ─► OPEN_PROTECTED ─► CLOSING ─► CLOSED
                  │               └─► ENTRY_NOT_FILLED   (IOC accepted, zero fill)
                  ├─► ENTRY_VENUE_REJECTED
                  └─► ENTRY_UNKNOWN (timeout) ─ reconcile by client id ─► ack path above
                                               └─► ENTRY_NOT_ACCEPTED (N not-found checks, no exposure)
RESERVED / UNKNOWN + CANCEL_ENTRY with nothing filled ─► ENTRY_CANCELLED
```

The entry outcome is recorded separately: `VENUE_REJECTED | NO_FILL | PARTIAL_FILL |
FULL_FILL | UNKNOWN | NOT_ACCEPTED`. An accepted order is never treated as a fill; fills come
from venue fill reports, de-duplicated by `(account, venue_fill_id)`.
Trade-level status: `ACTIVE`, `CLOSE_REQUESTED`, `CANCEL_REQUESTED`, `TOMBSTONED`.
Order status: `INTENT` (persisted before submission) → `OPEN | FILLED | CANCELLED | EXPIRED |
REJECTED`, or `UNKNOWN` → reconciled → `NOT_ACCEPTED`.

## Key policies

**Sizing.** `target_notional = M × L`; contracts = `floor_to_step(M × L / P / multiplier)`.
`P` is the IOC ceiling for LONG — an upper bound of the fill price, so planned notional
never exceeds `M × L`. For SHORT, `P = max(best bid, reference entry)`; this is **not** an
upper bound (a favourable higher-price fill raises notional at fixed BTC quantity). After
fills, requested margin, planned quantity, actual notional, actual margin, fees and unused
allocation are recorded separately. A breach of a configured hard limit
(`max_initial_margin_hard`, `max_notional_hard`) blocks the account and emergency-closes the
offending exposure; leverage is never changed and no collateral is added. Partial fills are
never topped up. `max_loss_at_stop` (optional) rejects entries using the worst-case IOC price;
it never moves the SL or reduces leverage.

**Reservations.** Inside one `BEGIN IMMEDIATE` transaction: `available − Σ reservations of
RESERVED/SUBMITTED/UNKNOWN executions on the account ≥ planned_margin × (1 + buffer) +
2 × taker_fee × planned_notional`, plus an overlap re-check, then the execution row is
inserted. Reservations are released as soon as the entry outcome is known.

**Price eligibility.** Adverse bound only (LONG `E + D` floored to tick; SHORT `E − D`
ceiled). Best ask/bid beyond the bound → `ENTRY_PRICE_ADVERSE`. Favourable prices are
accepted when the stop is on the correct side of the expected fill (depth VWAP) **and** of
the current declared trigger-reference price, and every TP is beyond both. Re-checked
against the actual average fill; invalid post-fill geometry → emergency-close path. Quotes
older than `max_quote_age_seconds`, spreads wider than `max_spread_fraction`, signals older
than `max_signal_age_seconds` or dated beyond `max_clock_skew_seconds` are rejected.

**Protection (desired-state reconciler).** Every cycle, for open exposure: sync all exit
orders by client id, read the venue position, then converge to one reduce-only stop for the
*actual* position quantity at the authoritative stop, plus one reduce-only TP per unfilled
leg. Stop resizing after TP fills uses amend (when the venue declares it) or
place-new-then-cancel-old; coverage is never dropped first, and reduce-only semantics mean
overlapping exits cannot increase or reverse exposure. A protective placement that is
rejected, or whose timeout cannot be verified by an immediate query, raises a
`PROTECTION_FAILED` incident, blocks the account and starts a reduce-only emergency close.
A take-profit placement failure while the stop is verified is a *degraded* state
(`TP_PLACEMENT_FAILED` alert, retried every cycle), not an emergency close. A protective order
that is not observed on the venue is re-checked up to `unknown_resolution_checks` times
before a replacement is placed, so a stale read never creates duplicate stops. Native
attached brackets are not used in Phase 1.

**TP allocation.** Fractions sum to exactly 1. All legs but the last get
`floor_to_step(Q × f)`; the last absorbs the residual; every leg must be ≥ venue minimum.
If a partial fill makes this infeasible → emergency close (`EXIT_PLAN_INFEASIBLE`, warning,
no account block). Amendments re-allocate the **remaining** position across the new legs;
executed legs are preserved in `execution_log`.

**Entry-relative levels** (only when the signal says `ENTRY_RELATIVE` *and* the account
allows it). Computed from the VWAP of all entry fills once the IOC is terminal (the
simulated IOC is atomic, so protection is placed only on the final VWAP — no interim
plan exists); stop and targets are rounded *toward* the entry so neither distance widens.
Never inferred from a missing field or from source risk.

**Closing.** `CLOSING` keeps existing stops (resized to the remaining quantity when amend is
available) until the venue reports flat; reduce-only market close attempts continue after
partial fills; an UNKNOWN close is reconciled (bounded not-found checks) before another
attempt. If a close attempt fails and no stop is working (e.g. an emergency close of a fresh
fill), a reduce-only stop at the authoritative level is placed so the remaining exposure is not
left unprotected; failure to do so raises `UNPROTECTED_WHILE_CLOSING` (critical). A venue
position on the opposite side raises `POSITION_MISMATCH` and stops order attempts (manual
intervention). On flat: a final sync queries every order of the execution so no fill is
missed in the books; cancel orphaned exits, book realized P&L and fees, resolve that
execution's incidents (the account block stays until an operator `account unblock`). The
recorded close reason is what actually flattened the position (e.g. `TAKE_PROFIT` when
exchange-held TPs beat a requested close).

**Time exits.** `close_at_utc` (absolute) or `max_holding_seconds` from the first actual
fill; ambiguous combinations are rejected. Evaluated every cycle; after an outage an overdue
deadline is closed immediately and alerted `TIME_EXIT_OVERDUE`. Entry expiry never affects
management of open exposure.

**Startup / periodic reconciliation.** Before new entries, every account's positions and
working orders are compared with the journal. Unowned exposure or unknown working orders →
incident + entry block; never silently adopted. Then all active executions are managed
(unknown entries resolved, exchange-side exits discovered, overdue deadlines handled).

**Isolation and serialization.** One worker process (OS file lock) handles accounts
sequentially, which serializes all actions per account/instrument. Each execution and each
event has its own error handling (adapter errors and unexpected exceptions → alert/incident,
continue), and a failed cycle never stops the worker, so an unresolved account never blocks
protection on healthy accounts. A failed journal `COMMIT` is rolled back so the connection
stays usable.

**Configuration.** Reloaded when the file changes (`run`). Each execution stores a policy
and instrument snapshot; updates/closes route to the trade's recorded executions only
(including accounts later disabled; never newly added accounts). Owner reassignment and
account removal are rejected while exposure, pending entries, live orders or incidents
remain.

## Journal tables
`events` (canonical JSON + hash, source/received timestamps, processing outcome),
`ingest_log`, `trades` (routing snapshot, revision, tombstones), `executions` (plan,
reservations, fills, fees, risk, deadlines, policy/instrument snapshots), `orders`
(deterministic client ids, intents persisted before submission), `submissions`, `fills`,
`execution_log`, `accounts`, `controls`, `incidents`, `alerts`, `reconciliations`,
`config_snapshots`, `balance_snapshots`. Deduplication scope: `(account_id,
client_order_id)` and `(execution_id, leg, leg_index, plan_version, attempt)`. This gives
local idempotency; it is **not** an exchange-side exactly-once guarantee — recovery always
queries the venue by persisted identity.

## Simulator assumptions (synthetic, not a model of any real exchange)
One-way position mode; isolated margin = notional / leverage; flat taker fee per venue on
every fill; IOC limits walk the scripted book and consume liquidity; trigger orders fire on
their declared reference price (stops before TPs in the same tick) and fill as reduce-only
market orders, extending at the last level price if the book is exhausted; **no liquidation
and no funding**. Fault injection: `REJECT`, `TIMEOUT_BEFORE_ACCEPT`, `TIMEOUT_AFTER_ACCEPT`,
`NO_FILL`, `PARTIAL`, `SHIFT_BOOK`, `STALE_NOT_FOUND`, `DUPLICATE_FILLS`. Its state is
independent of the journal and persists across worker restarts; the harness can move the
market (triggering exchange-held exits) while the worker is down.
