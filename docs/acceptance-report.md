# Phase 1 acceptance report

**Scope:** simulation-only execution foundation. **Date:** 2026-11-16.
**Environment:** Windows 11, Python 3.12.10, repository `.venv`, no network access used by
tests or demo, no API keys.

## Commands and observed results

| Command | Result |
|---|---|
| `.\.venv\Scripts\python.exe -m pytest` | **79 passed** (~7 s) |
| `.\scripts\aseke-exec.ps1 scenario run all` | **27/27 scenarios passed** |
| `.\.venv\Scripts\python.exe -m pyflakes src tests` (local lint tool, not a project dependency) | clean |
| README demonstration via `.\scripts\aseke-exec.ps1` (validate-config → sim init → publish-example long → cycle → status → sim set-market 66100 → cycle → status) | both accounts `OPEN_PROTECTED` (alpha 7 @ 64500.1, margin 90.30014, risk@SL 3.5; beta 75 @ 64500.05, margin 96.750075, risk@SL 3.749625), then both `CLOSED (TAKE_PROFIT)` |

Test files: `tests/test_planning.py` (pure math), `tests/test_contract.py` (every documented
valid/invalid example, JSON-Schema agreement, canonical hashing),
`tests/test_scenarios.py` (all lifecycle scenarios), `tests/test_config_inbox_cli.py`
(config validation, atomic publisher, end-to-end CLI demo).

An ECC `python-reviewer` pass over the engine found no duplicate/reversed-exposure path and
flagged lifecycle gaps; the valid ones were fixed and are covered by the last three
scenarios (deferred amendment retry, TP failure handled as degraded, stale stop observation)
and by the extended `hard_limit_breach_short` (stop kept while a failing close is retried).

## Required acceptance scenarios → evidence

| # | Requirement | Evidence |
|---|---|---|
| 1 | LONG ceiling / SHORT floor signs; boundary + tick rounding preserve adverse limit | `test_reference_bounds_have_correct_signs` (64600.10 / 64400.10), `test_tick_rounding_preserves_adverse_limit`, `test_boundary_price_is_inclusive…`; scenarios `long_reference_two_venues`, `short_reference_stop` |
| 2 | Favourable > tolerance accepted; favourable through stop rejected, stop unchanged | `price_eligibility` (200 favourable → filled 64300.1, stop 64000.10; 63900 → `STOP_ALREADY_BREACHED`, no entry order) |
| 3 | 100 × 5 ≈ 500 target notional; multipliers give comparable exposure | `test_margin_100_leverage_5…`; `long_reference_two_venues` (7 contracts = 0.007 BTC vs 75 = 0.0075 BTC) |
| 4 | Unsupported leverage, cross-only, insufficient collateral, wrong owner, overlap rejected | `preflight_rejections` (`LEVERAGE_UNSUPPORTED`, `MARGIN_MODE_UNSUPPORTED`, `INSUFFICIENT_COLLATERAL`, `STRATEGY_NOT_OWNER`, `OVERLAPPING_TRADE`) |
| 5 | Absolute SL/TP unchanged; entry-relative from actual fill, LONG and SHORT | `long_reference_two_venues`, `test_absolute_levels_are_returned_unchanged`; `entry_relative` + `test_entry_relative_long_and_short…` |
| 6 | "0.2%" metadata never redefines stop or caps | `source_risk_informational` |
| 7 | IOC no-fill / partial / full / unknown distinct; partial protected, not topped up | `ioc_outcomes`, `entry_timeout_after_accept_recovery`, `entry_timeout_before_accept` |
| 8 | TP fractions + minimums; residual stop coverage; no reversal | `multi_tp_and_competing_exits`, `partial_infeasible_emergency`, allocation unit tests, `close_paths` |
| 9 | Duplicate / conflicting / incomplete files, crash around ingestion | `event_ordering`, `crash_around_ingestion`, `test_publisher_is_atomic…` |
| 10 | Crash/timeout after acceptance recovered from venue state | `entry_timeout_after_accept_recovery`, `cancel_entry_race`, `close_paths` |
| 11 | Failed protection → close/reconcile + block | `protection_failure`, `hard_limit_breach_short` |
| 12 | Overdue holding deadline after restart; downtime fills discovered | `downtime_stop_and_deadline` |
| 13 | Repeated CLOSE, stale AMEND, missing predecessor, CLOSE before OPEN | `event_ordering`, `close_paths`, `amend_retry_after_adapter_error` |
| 14 | One account succeeds while another rejects/uncertain; independence | `protection_failure`, `entry_timeout_after_accept_recovery`, `ioc_outcomes`, `unowned_exposure` |
| 15 | Pause / disable affect entries only | `pause_and_disable` |
| 16 | Config changes affect future entries; original routing retained | `config_change_routing` (incl. owner reassignment rejected) |
| 17 | Live mode and unsupported product/currency fail clearly | `live_and_unsupported`, `test_invalid_config_fails_clearly` |
| 18 | Windows demo reproducible offline | `test_documented_cli_demo_end_to_end` + the PowerShell run above |

## Not run / not verified
- No runs on Linux/macOS (the core is platform-neutral; the POSIX `fcntl` lock path is untested).
- `run` (long-running foreground worker) was exercised only through the same engine cycle
  code; no soak test.
- No concurrency test with two real OS processes beyond the lock-acquisition scenario.
- Nothing was verified against any real exchange, sandbox or current fee schedule.

## Limitations
- **Synthetic venues only.** No liquidation, ADL or funding model; flat taker fees; trigger
  orders assume liquidity at the last book level. These do not validate any real exchange's
  rules, fees or liquidation calculations.
- Native attached (bracket) protection is not implemented; the verified separate
  protective-order flow is always used.
- Entry-relative levels assume a terminal IOC (true in the simulator). An adapter that
  reports fills incrementally on a still-working order would need interim-protection logic
  before entry-relative mode is enabled for it.
- SHORT sizing uses `max(best bid, reference entry)`, which is not an upper bound on fill
  notional; breaches are caught only after the fill by the hard-limit emergency-close path.
- Amendments must be ABSOLUTE; source-risk enforcement is not implemented (informational
  only); account-equity risk limits are not implemented.
- A TP that keeps failing (e.g. price already beyond it) leaves the position stop-protected
  with repeated `TP_PLACEMENT_FAILED` alerts; no automatic re-pricing.
- Single worker process; accounts are processed sequentially per cycle.
- Producer identity is a configured name, **not authentication**.
- Alerts are local (journal + `var\alerts.jsonl` + log); no Telegram/dashboard.

## Readiness statement
Phase 1 delivers a working, deterministic **simulation-only** execution lifecycle with
durable journaling, recovery and operator controls, verified by the tests and scenarios
above. It is **not** production-ready, has **not** been validated against any real
exchange, and makes **no** claim about profitable trading.
