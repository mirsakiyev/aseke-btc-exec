# Signal / event contract v1 — `aseke.exec.signal/1`

Machine-readable structural schema: [`schemas/signal.v1.schema.json`](../schemas/signal.v1.schema.json).
Examples: [`examples/signals/valid`](../examples/signals/valid) and
[`examples/signals/invalid`](../examples/signals/invalid) (`EXPECTED.json` maps each invalid
file to its reason code). Example timestamps are **scenario fixtures** (2026-01-05T12:00Z);
use `aseke-exec publish-example` for signals stamped with the current clock.

## Rules
- Strict: unknown fields are rejected (`UNKNOWN_FIELD`). Credentials, balances and account
  selection are **not** part of the contract — routing and risk limits belong to Exec config.
- Prices, fractions and amounts are **decimal strings** (`"64500.10"`); JSON numbers,
  `NaN`/`Infinity` are rejected. `leverage`, `revision`, `max_holding_seconds` are integers.
- Timestamps are ISO-8601 with an explicit zero UTC offset (`Z` or `+00:00`).
- `environment`: `SIMULATION` or `LIVE`. **LIVE is refused in Phase 1.**
- `producer.name` must be configured as trusted. A name is not authentication.
- `instrument` is the normalized target: `base` (BTC only), `quote`, `settle`,
  `product_type` (only `LINEAR_PERPETUAL`; quote must equal settle). `source.quote_currency`
  must equal `instrument.quote` unless an explicit mapping is configured (USD ≠ USDT).
- Exactly one action payload, matching `event_type`: `open`, `amend`, `close`, `cancel_entry`.

## Common fields
`schema_version`, `environment`, `producer{name, instance?}`, `event_id` (immutable),
`trade_id` (stable), `strategy_id`, `strategy_version`, `event_type`, `revision`,
`created_at`, `instrument`, `source?` (required on OPEN: `venue, instrument,
reference_price_type (LAST|MARK|INDEX), quote_currency, data_time`).

## OPEN (`revision` = 1)
```json
"open": {
  "direction": "LONG", "leverage": 5, "entry_intent": "IMMEDIATE_IOC",
  "reference_entry": "64500.10", "expires_at": "…Z",
  "exits": {"level_policy": "ABSOLUTE", "trigger_reference": "LAST",
            "stop": {"price": "64000.10"},
            "take_profits": [{"price": "66000.10", "quantity_fraction": "1"}]},
  "holding": {"max_holding_seconds": 3600}
},
"source_risk": {"risk_fraction": "0.002", "basis": "SOURCE_PAPER_EQUITY", "note": "…"}
```
- Geometry vs reference entry: LONG `stop < E < every TP`; SHORT `stop > E > every TP`.
- TP `quantity_fraction`s sum to exactly 1 (1–10 targets, distinct levels).
- `ENTRY_RELATIVE`: `stop.distance_fraction` and per-TP `distance_fraction` (0<d<1), computed
  from the **actual** VWAP fill; only if the account enables it. Never inferred.
- `holding`: exactly one of `close_at_utc` or `max_holding_seconds` (from the first fill);
  omitted = no time exit. `expires_at` applies to entry only.
- `source_risk` is informational; it never moves the stop or caps sizing.

## AMEND_EXITS (`revision` ≥ 2)
`"amend": {"exits": {ABSOLUTE exit plan}, "holding": {...}}` (at least one). Replaces the
**remaining** exit plan: TP fractions apply to the remaining position quantity; executed legs
are preserved. Valid if the stop and targets are on the correct sides of the current trigger
reference price (a stop may move beyond the original entry, e.g. profit-protecting). Never
changes quantity, entry price, margin mode or leverage, and never creates a position. Before
the entry resolves, it replaces the plan that will be applied at fill time.

## CLOSE / CANCEL_ENTRY (`revision` ≥ 2)
`"close": {"reason": "…"}` closes existing exposure on every execution recorded for the
trade (including accounts since disabled; never newly configured accounts).
`"cancel_entry": {"reason": "…"}` cancels unfilled entries; any fill discovered by
reconciliation is protected and managed (send CLOSE to exit it).

## Ordering and idempotency policy
| Situation | Behaviour |
|---|---|
| Same `event_id`, same canonical content | `DUPLICATE` — no second effect (file archived) |
| Same `event_id`, different content | `EVENT_ID_CONFLICT` — quarantined + alert |
| Second OPEN for an existing `trade_id` | `DUPLICATE_OPEN_CONFLICT` |
| AMEND with `revision` ≤ applied | `STALE` — ignored |
| AMEND hit a transient adapter error on some account | stays `PENDING` and is retried; executions already amended are skipped (idempotent per execution) |
| Revision gaps | allowed (each AMEND is a complete remaining plan) |
| CLOSE / CANCEL_ENTRY | monotone (only reduce exposure): applied regardless of order; repeats are no-ops |
| CLOSE / CANCEL_ENTRY before OPEN | durable **tombstone**; a delayed OPEN → `TRADE_TOMBSTONED` |
| AMEND before OPEN | `DEFERRED` up to `missing_predecessor_hold_seconds` (default 300), then `MISSING_PREDECESSOR` |
| OPEN past `expires_at`, too old, future-dated beyond skew | per-account `SIGNAL_EXPIRED` / `SIGNAL_TOO_OLD` / `CLOCK_SKEW` |

## Reason codes
All codes are defined in [`src/aseke_exec/reasons.py`](../src/aseke_exec/reasons.py) and
recorded on events (`process_code`), ingest log entries and executions (`reason_code`).

## Future Lab publisher (integration guide)
Lab keeps its own **outbox** (in Lab's database, written in the same transaction as its
decision). A Lab-side relay then, for each outbox row: renders the v1 JSON with a stable
`event_id` (e.g. `<trade_id>-r<revision>-<type>`), writes it to a temp file in Exec's inbox
directory on the **same volume**, flushes/fsyncs, renames it into place without overwriting
(`aseke_exec.inbox.publish_event` is the reference implementation), and marks the outbox row
published. Re-publishing the same row is safe (duplicates have no second effect). Exec never
reads Lab's database, and the contract is the only coupling — no execution logic changes.
Before any live phase the producer must be authenticated (e.g. signed envelopes with a key
Exec trusts); a producer name alone is not sufficient.
