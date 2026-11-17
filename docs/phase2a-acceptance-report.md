# Phase 2A acceptance report — audit, recovery, protection integrity, authenticated ingestion

**Date:** 2026-11-16. **Scope:** incremental hardening of the simulation-only executor.
Not a real-exchange integration; **not** live-trading approval or profitability validation.

## 1. Baseline (reproduced before any change)
| Item | Value |
|---|---|
| Source revision | git `9b4501f` (Phase 1 commit), clean working tree |
| Python | 3.12.10 (`C:\Users\16477\Projects\aseke-btc-exec\.venv\Scripts\python.exe`, MSC v.1943 64-bit) |
| Embedded SQLite | **3.49.1**, source id `2025-02-18 13:38:58 873d4e27…0e70` |
| Dev dependencies | pytest 9.2.0, jsonschema 4.26.0, pyflakes 4.1.0 (local lint) |
| Baseline results | pytest **79 passed**; `scenario run all` **27/27** (the same 27 scenario functions also run inside pytest — overlapping, not independent); pyflakes clean |
| User runtime | `var\` SHA-256 fingerprints identical before and after the baseline runs (all harnesses use temp roots) |

Runtime fingerprint after the work: `runtime-info` (source fingerprint, config hash, git head,
redacted auth profile). Config fingerprints are the `source_hash` of `config/sim.toml` /
`config/sim-strict.toml` reported there; no secrets are included.

## 2. SQLite runtime finding (unresolved upstream; mitigated in-project)
Official guidance (https://sqlite.org/wal.html, "WAL-reset bug", page dated 2026-08-25): affects
3.7.0–3.51.2; fixed in 3.51.3; backports 3.44.6 and 3.50.7. Trigger: ≥2 connections in separate
threads/processes writing or checkpointing a WAL database at the same instant; consequence is
possible corruption. 3.49.1's release notes contain no WAL fix → **AFFECTED** (classifier:
`ops.wal_reset_status`, tested for 3.44.5/3.44.6/3.49.1/3.50.6/3.50.7/3.51.2/3.51.3/3.52.0).

Topology review (before): the worker held one journal and one simulator connection, but
operator commands (`pause`, `account …`) opened a **second journal writer** while a worker ran,
and `sim set-market/fault` opened a **second simulator writer** — both WAL. After:
journal single-writer (lock or queued control request), read-only `mode=ro` readers (verified
on this runtime: reads work, writes refused, checkpoint fails), simulator in rollback-journal
mode. **Residual:** the library remains affected; unattended operation is not certified.
Remediation plan (operator decision, nothing replaced silently): project-isolated Python/venv
whose bundled SQLite is ≥ 3.51.3 (or 3.50.7 / 3.44.6), verified via `runtime-info`; global
Python used by aseke-btc-lab untouched. Side effect observed: my one read-only open of the
user's `var\journal.db` (for the migration check) left empty `-wal`/`-shm` sidecars there; the
main database file hash is unchanged; the sidecars were deliberately not deleted.

## 3. Findings and fixes (verified)
Classification: **D** = reproduced/confirmed defect (fixed + regression test), **L** = intentional
limitation (documented), **U** = unverified concern.

| # | Class | Finding | Fix / evidence |
|---|---|---|---|
| 1 | D | Duplicate JSON keys silently accepted (last wins) despite "strict" contract | `contract.load_event_json` rejects `DUPLICATE_JSON_KEY`; `test_duplicate_json_keys_rejected_in_unsigned_events_too`, auth dup-key test |
| 2 | D | Crash after the trade row but before routing every account → retried OPEN rejected `DUPLICATE_OPEN_CONFLICT`, remaining accounts never routed | `_apply_open` resumes the ORIGINAL routing for accounts without an execution (full preflight); `test_crash_after_intent_before_submission_never_submits_late`, `test_crash_after_venue_fill_before_local_ack` (resumed beta correctly refused `ENTRY_PRICE_ADVERSE` after the market moved) |
| 3 | D | Hot config reload dropped the runtime root (`load_config(path)` without data dir) | reload keeps `data_dir`; `--data-dir` override added |
| 4 | D | Archive failure raised out of ingestion and stalled the whole cycle | moves are non-fatal alerts; stages isolated; `test_archive_failure_does_not_block_processing` |
| 5 | D | Protective order demoted after ONE missing observation right after a timeout | stays UNKNOWN; bounded re-checks; `test_delayed_observation_after_timeout_is_not_treated_as_absent` |
| 6 | D | `not_found_checks` never reset → lifetime (not consecutive) misses could demote a live stop | reset on every sighting; `test_not_found_counter_resets_when_order_is_seen_again` (ECC review) |
| 7 | D | Stop-resize failure triggered an emergency close although the old reduce-only stop still covered | oversize stop = verified coverage, bounded resize retry; `test_stop_resize_failure_keeps_the_covering_stop` |
| 8 | D | Failed cancel of an old stop after resize could re-resize every cycle (churn) | best-fit current stop; `test_failed_cancel_of_old_stop_after_resize_does_not_churn` (ECC review) |
| 9 | D | TP retry had no backoff/escalation; no separate TP coverage | `stop_coverage`/`tp_coverage`, exponential backoff (5→300 s), `TP_PLACEMENT_PERSISTENT` incident, `ACCOUNT_DEGRADED` gate; `test_persistent_tp_failure_bounded_backoff_and_durable_escalation`, `tp_failure_degraded` scenario |
| 10 | D | Per-leg TP progress not durable; partial TP fill unaccounted | `exit_plan.legs[].executed_qty/remaining_qty` from fills, final at close; `test_partial_tp_fill_is_accounted_per_leg_and_not_rearmed` |
| 11 | D | Plan-version transition could over-allocate TPs (old TPs cancelled after new placed) | old-version TPs retired first, new legs capped at position (ECC review) |
| 12 | D | CLOSING path never demoted an unresolved stop; undersized stop left stale "VERIFIED" | bounded demotion + UNVERIFIED marking; `test_unresolved_stop_while_closing_is_bounded_and_reported` |
| 13 | D | Failed emergency close without placeable stop only alerted | durable `UNPROTECTED_EXPOSURE` incident, measured interval; `test_failed_emergency_close_without_valid_stop_raises_incident_until_resolved` |
| 14 | D | Unprotected interval not measured | `unprotected_since` / `max_unprotected_seconds` (also when exposure ends flat) |
| 15 | D | Stray cancel could cancel an order still inside its re-check window | open-order sightings are applied, not cancelled (ECC review) |
| 16 | D | A failed SQLite COMMIT could leave the connection inside a transaction | rollback on failed COMMIT; `test_commit_failure_is_rolled_back_and_connection_stays_usable` |
| 17 | D | One SQLite failure in `manage_all` aborted all accounts | per-execution isolation; events stage stops on SQLite errors |
| 18 | D | Health on an un-migrated v1 journal would KeyError | schema check; additive v1→v2 migration test against the real v1 schema from git (`tests/fixtures/journal_v1_schema.sql`) and a backup-API copy of the user's v1 journal (2 executions preserved) |
| 19 | D | Health claimed "running" after a one-shot `cycle` | heartbeat `kind` + non-intrusive PID check (`OpenProcess`, never `os.kill` on Windows); `test_health_reports_a_resident_worker_and_its_absence` |
| 20 | D | Memory probe leaked a ctypes type per call (inflated soak memory 28→56 MB) | module-level type; tracemalloc re-measure: heap +0.4 MB / 1200 cycles |
| 21 | D | Security review: poison inbox files (invalid UTF-8, 100k nesting, lone surrogate) stalled ingestion | per-file quarantine; `test_poison_files_are_quarantined_and_never_stall_the_inbox` |
| 22 | D | Security review: malformed control file halted all cycles / crashed worker at start | validation, `control\rejected\`, isolated drain; `test_malformed_control_requests_are_set_aside_without_stalling_cycles` |
| 23 | D | Security review: revoked-key CLOSE could tombstone; scope only checked at ingestion; reload could switch to unsigned profile | apply-time scope re-check, no tombstone from invalid keys, profile change requires restart; `test_revoked_key_close_cannot_tombstone_and_scope_is_rechecked_at_apply`, `test_hot_reload_cannot_switch_auth_profile` |
| 24 | D | Security review: BOM key files fail; HELD files not durable; gen-key permissions | BOM-tolerant load, one HELD `ingest_log` row, exclusive owner-only creation + location warning |
| 25 | L | SHORT sell floor is not an upper notional bound | planned vs actual margin recorded; `ACTUAL_MARGIN_ABOVE_PLAN` alert; hard-limit close path preserved; LONG sized at ceiling (upper bound) — `test_long_fill_above_estimate_stays_within_plan`, `test_short_favourable_fill_above_plan_is_reported_not_concealed`, `hard_limit_breach_short` |
| 26 | L | TP partially filled with price already beyond its level cannot be re-armed until price retraces | remainder stays stop-protected, DEGRADED, backoff + escalation (Phase 1 retain-and-retry kept; no invented market exit) |
| 27 | L | Control queue is authenticated only by filesystem access to `control\` | runbook: operator-only ACL; it cannot create trades |
| 28 | L | Replay of captured non-OPEN envelopes within 24 h if events were pruned/restored | events never pruned; documented |
| 29 | U | p95 cycle latency ~100 ms in 3000-cycle stress (first/last-10 means 36→18 ms, i.e. spikes, not monotonic growth); not root-caused | needs the operator soak |

Unchanged on review: no path to duplicate or reversed exposure was found by either ECC review;
healthy positions produce **no venue writes** (`test_healthy_unchanged_position_generates_no_order_churn`:
25 cycles, zero submissions). Reviews do not prove the absence of all defects.

## 4. Requirement → evidence matrix
| Requirement | Evidence (file::test) | Overlap |
|---|---|---|
| Crash after ingestion, before archive | test_process_crash::test_crash_after_durable_ingestion_before_archive | Phase 1 `crash_around_ingestion` (in-process) |
| Crash after intent, before submission | ::test_crash_after_intent_before_submission_never_submits_late | — |
| Crash after venue fill, before ack | ::test_crash_after_venue_fill_before_local_ack | Phase 1 timeout scenario (in-process) |
| Crash during partial-fill protection | ::test_crash_during_partial_fill_protection | — |
| Crash during protection amendment | ::test_crash_during_protection_amendment | — |
| Crash after close fill, before accounting (2 points) | ::test_crash_after_close_fill_before_local_accounting[…] | — |
| Durable tombstones across restart | ::test_close_before_open_tombstone_survives_restart | Phase 1 `event_ordering` |
| Dual-worker exclusion, stale lock, queued operator action | ::test_competing_workers_operator_queue_and_stale_lock | Phase 1 `worker_lock` (in-process) |
| SQLite busy / commit / intent / post-ack failures | test_phase2a_lifecycle::test_journal_busy…, ::test_commit_failure…, ::test_intent_persistence_failure…, ::test_persistence_failure_after_venue_ack… | — |
| Inbox/archive failure | ::test_archive_failure_does_not_block_processing | — |
| No healthy-cycle churn | ::test_healthy_unchanged_position_generates_no_order_churn | — |
| Partial TP accounting | ::test_partial_tp_fill_is_accounted_per_leg_and_not_rearmed | Phase 1 `multi_tp_and_competing_exits` |
| Degraded TP behaviour | ::test_persistent_tp_failure…, scenario `tp_failure_degraded` | — |
| Stop replacement failure / delayed observation / stale exit after flat | ::test_stop_resize_failure…, ::test_delayed_observation…, ::test_stale_exit_after_flatness_cannot_reverse, ::test_not_found_counter…, ::test_failed_cancel… | scenario `stale_stop_observation` |
| Failed emergency close & unprotected interval | ::test_failed_emergency_close…, ::test_unresolved_stop_while_closing… | scenario `hard_limit_breach_short` |
| Sizing both directions + hard limit | ::test_long_fill_above_estimate…, ::test_short_favourable_fill…, scenario `hard_limit_breach_short` | — |
| One failing account cannot starve another | ::test_failing_account_cannot_starve_a_healthy_account | Phase 1 `protection_failure` |
| Strict auth happy path / rejections / replay / poisoning / scope / revocation / rotation / missing key / timestamps / fixture | test_auth.py (23) | — |
| Backup/restore to new root, tamper, refusal while running | test_ops::test_coordinated_backup_and_restore_to_new_root (+ PowerShell run) | — |
| Reset safety / fresh init | test_ops::test_fresh_runtime_and_reset_safety | — |
| Health / heartbeat | test_ops::test_health…, test_process_crash::test_health_reports_a_resident_worker… | — |
| Bounded soak | test_ops::test_accelerated_soak… (400 cycles) + runs below | — |

## 5. Commands and observed results (final code)
| Command | Result |
|---|---|
| `.\.venv\Scripts\python.exe -m pytest` | **149 passed** in 52.5 s (79 Phase 1 retained + 70 new; the 27 scenario tests duplicate `scenario run all`) |
| `.\scripts\aseke-exec.ps1 scenario run all` | 27/27 |
| `.\.venv\Scripts\python.exe -m pyflakes src tests` | clean |
| Real-time smoke `soak --duration-seconds 90 --poll-seconds 0.5` (`runtime\soak-smoke-20261116183627`) | **measured elapsed 90.4 s wall**, 169 cycles, 22 signals, 4 closed + 2 open protected executions, 38 `OVERLAPPING_TRADE` rejections (by design: new OPEN every 8 cycles), 0 incidents, **0 invariant violations**, cycle latency p50 12.7 / p95 28.5 / max 170 ms, working set 28.6→30.5 MB, journal+WAL 0.40→4.32 MB |
| Accelerated stress `soak --cycles 3000 --accelerated --poll-seconds 1` (`runtime\stress-accel-final-…`) | injected clock, **not a real-time soak**: 153.9 s wall, 375 signals, 290 closed executions, 460 overlap rejections, 0 incidents, 0 violations, p50 15.3 / p95 101.3 / max 210 ms, working set 28.5→32.9 MB, journal 0.40→7.74 MB, simulator 0.08→0.61 MB |
| Strict signed demo (PowerShell, `runtime\strict-demo-20261116183456`) | signed OPEN executed + protected on both accounts; unsigned file quarantined `AUTH_REQUIRED`; `auth check` false→true after loading the env var; key value not found in the journal files |
| Backup → restore → cycle (same demo) | manifest generation `b99b0e59…`, quiesced; restored root reconciled `OK/OK` before entries, positions still `OPEN_PROTECTED` |
| README unsigned demo (`runtime\demo-20261116185551`) | open → protected → TP → CLOSED; `sim init --reset` refused (exit 2) |

**Not run:** the extended (e.g. 24 h) operator soak; Linux/macOS; host power-loss or disk-full
tests (process-crash tests only); multi-hour worker run; concurrency with more than two real
processes; any real exchange, sandbox or network test; Lab integration (none exists).

## Limitations
- Synthetic venues only; no liquidation, ADL or funding model; flat fees; trigger fills assume
  liquidity at the last book level. Real venues remain out of scope.
- Embedded SQLite 3.49.1 affected by the WAL-reset bug (mitigated by topology, not fixed).
- HMAC = shared-key possession + integrity only; no encryption, no asymmetric origin proof, no
  protection against a compromised Windows account/host; key file ACLs on Windows are manual.
- The control queue is protected only by filesystem permissions on `control\`.
- SHORT fill notional has no pre-trade upper bound (post-fill hard-limit path only).
- Crossed partial TPs wait (stop-protected) for price to retrace; no automatic market exit.
- Separate entry/protective orders (no native brackets): a measured unprotected interval exists
  between first fill and verified stop; it is recorded per execution, not eliminated.
- Single worker per runtime root; accounts processed sequentially per cycle.
- Producer names are still not authentication in the compatibility profile.

## Readiness verdict
**Eligible for an attended operator soak in an isolated runtime root** (functional, crash-
recovery, auth and protection evidence above; 0 invariant violations in the measured runs).
**Blocked for unattended operation** until the SQLite WAL-reset-bug runtime concern is
resolved with a project-isolated fixed SQLite (and an extended soak has been run and reviewed).
This is **not** live-trading approval and not a profitability validation.
