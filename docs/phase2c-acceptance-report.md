# Phase 2C acceptance report — soak workload corrections and closeout

**Date:** 2026-11-18 · **Workstation:** Windows 11 Pro 10.0.26300 (x64) · **Source:** HEAD
`9355dc2` plus the uncommitted Phase 2C working tree · **Runtime:** approved project-private
python.org CPython 3.15.2 (amd64), loaded SQLite 3.53.4, source id
`2026-07-24 19:02:57 bf7c7f30…59bcc` (`runtime_policy.operational = true`,
`approved_build_id python.org-cpython-3.15.2-amd64+sqlite-3.53.4`) · **Mode:** simulation only.

> **Verdicts (kept separate):**
> - **Original 24 h run.** Accepted as evidence of duration, stability, invariants and storage
>   integrity. It is **not** evidence of sustained entry throughput: 90 signals were invalid
>   because of a harness defect, and the 1,000 USDT synthetic wallets were exhausted after
>   cycle ~19,500.
> - **Corrected funded-load coverage.** Demonstrated in bounded runs (accelerated, plus a short
>   real-time run): see §6.
> - **Unattended simulation.** **Not approved.** That requires operator sign-off and, as
>   recommended here, one attended funded 24 h run (§9). `evidence.extended_soak` stays `PENDING`.
> - **Real exchange / live trading.** **Not approved.** Phase 3 **read-only** adapter work is
>   eligible to start (§10).

## 1. Original 24 h run — fingerprint and immutable evidence

| Item | Value (read from the evidence files; nothing rewritten) |
|---|---|
| Root | `runtime\soak-24h-20261116-213501\` |
| Window | 2026-11-17T02:35:02.372Z → 2026-11-18T02:35:04.545Z (21:35 EST to 21:35 EST) |
| Status / exit | `COMPLETED`; operator-observed exit code 0 |
| Elapsed (wall) | 86,402.045 s real-time, poll 2 s, seed 7, `trade_every_cycles` 8, holding 30 s |
| Auth | strict-signed, `aseke.exec.envelope/1` HMAC-SHA256, key id `sim-demo-k1` |
| Source / config | git head `9355dc2…`, source fingerprint `3085b1e4…99ae22`, `config\sim-strict.toml` hash `5f4f07b4…3f58da` |
| Cycles / signals | 32,702 cycles checked; 4,088 signals published |
| Ingest | JOURNALED 3,998 + INVALID 90 = 4,088. Files: inbox 0, archive 3,998, quarantine 90 (all `INVALID_NUMBER`) |
| Executions | 7,996 = CLOSED 2,849 + `OVERLAPPING_TRADE` 2,059 + `INSUFFICIENT_COLLATERAL` 3,088 (labelled `unexpected_review`; kept as recorded) |
| Invariants / incidents / critical | 0 / 0 open / 0 |
| Protection | max entry→verified-stop 0.067 s across 2,849 executions (measured, not eliminated) |
| Storage | journal WAL and simulator DELETE: `integrity_check ok`, 0 foreign-key violations; 0 open positions at the end |
| Latency | p50 20.5 ms, p95 143 ms, p99 294 ms, max 541 ms; working set 36.5 → 34.4 MB (max 37.1) |
| Backup | `runtime\soak-backup-20261118-213009\`, generation `2626d5ff7c694b3b84c5acfdebf87621`, quiesced, schema v2, SQLite 3.53.4. Rows: journal events 3,998 / executions 7,996 / orders 13,168 / fills 6,497; simulator orders 13,168 / fills 6,497 / positions 0 |

**Delayed console output.** The apparently lingering process at the end was the Windows Console
*Select* mode, which pauses a console's output. After leaving Select mode the console printed
`Soak exit code: 0` and returned to the prompt. This is not an engine deadlock. The runbook now
documents it.

**How Phase 2C touched this evidence.** Phase 2C read the 90 quarantined files and their
`.reason.json` (file reads only). It read the root's `soak-identity.json`, `soak-report.json` and
`soak-checkpoints.jsonl`. It made **one** read-only (`mode=ro`) pass over `journal.db` and
`sim_exchange.db` to collect the ledger and coverage. That WAL reader updated the timestamp of the
existing `journal.db-shm` (SQLite's shared-memory index). `journal.db`, `journal.db-wal` and
`sim_exchange.db` kept their size and mtime. No worker or writer was started, nothing was migrated,
reset, repaired, deleted or overwritten, and no sidecar was removed. The backup directory was not
opened by SQLite: only its `manifest.json` was read, because a read-only WAL open would have
created sidecars inside it. A backup is evidence of a runtime root; it is **not** a substitute for
committed source code.

## 2. Root cause A — the 90 `INVALID_NUMBER` signals (harness defect)

**Evidence.** All 90 `.reason.json` files say `'open.exits.take_profits[1].price' must be > 0`.
In every case the cause is the same field, and the signals share these properties:
* all 90 are **SHORT**, all fail on **TP index 1** (the second target);
* reference prices run from 1000 to 1455, and the TP2 values are negative (for example `-500` ×14,
  `-490` ×9);
* the first file is `soak-025992`, the last `soak-032496`.

**Mechanism.** The Phase 2A/2B soak loop floored the seeded random walk at 1000. It built SHORT
targets as `ref − 800` and `ref − 1500`, so for `ref ≤ 1500` TP2 was ≤ 0. The strict contract
rejected these correctly: `_dec(... positive=True)` gives `INVALID_NUMBER`. The engine and contract
behaved as designed; the generator was wrong. LONG levels (`ref − 500`, `ref + 800`, `ref + 1500`)
and SHORT TP1 stay positive across the walk's whole range.

**Deterministic replay** (`soakgen.replay`, no engine, no clock, 0.64 s). It uses seed 7, 32,702
cycles, `trade_every_cycles` 8 and initial price 64500.0, with the walk's RNG draws identical to the
old loop:

| Replay | Signals | Valid | Invalid | Notes |
|---|---|---|---|---|
| Legacy (pre-2C) levels | 4,088 | 3,998 | **90**, all `INVALID_NUMBER` | the trade-id list **exactly equals** the 90 historical quarantine files; LONG 2,077 / SHORT 2,011; price range 1000 – 98,205 |
| Corrected levels | 4,088 | 4,088 | **0** | same directions; 173 SHORT signals scaled (ref < 3000) |

The replay reproduces the historical candidates exactly, because the market walk never depends on
engine state. Five more seeds (1, 2, 3, 7, 42) at 40,000 cycles each give 5,000 signals and 0
malformed per seed. Seeds 1, 2 and 7 touch the 1000 floor.

## 3. Root cause B — `INSUFFICIENT_COLLATERAL` (capital exhaustion, ledger-based)

The ledger was read from the original simulator, read-only:

| Account | Start | Fees | Realized P&L | End wallet | Reconciles |
|---|---|---|---|---|---|
| `sim_alpha_a1` | 1,000.00 | −802.30168800 | −150.2204 | 47.47791200 | start + fees + P&L = wallet exactly |
| `sim_beta_b1` | 1,000.00 | −695.87734401 | −229.766800 | 74.35585599 | exactly |

One entry needs `margin × (1 + buffer) + fee reservation`, which is 100 × 1.05 + about 0.5–0.6,
or **≈ 105.6 USDT available**. Fees are about 0.57 (alpha) and 0.49 (beta) per round trip on
≈ 500 notional, and were 84 % (alpha) and 75 % (beta) of the depletion.

| Quarter (cycles) | alpha closed / refused | beta closed / refused |
|---|---|---|
| Q1 0–8174 | 594 / 0 | 594 / 0 |
| Q2 8175–16350 | 593 / 0 | 593 / 0 |
| Q3 16351–24525 | 230 / 626 | 245 / 598 |
| Q4 24526–32701 | **0 / 932** | **0 / 932** |

The last CLOSED trades were `soak-019504` (alpha) and `soak-019728` (beta). Every entry from
`soak-019520` (alpha) and `soak-019744` (beta) onward was refused for collateral. That is about the
last **40 %** of the run, roughly 13,200 cycles, with no executed entries.

The refusals were genuine protection, not an engine fault. Overlap is checked before collateral in
preflight, so each of these 3,088 refusals had no trade open. The classification remains
`unexpected_review` in the historical report, and Phase 2C does not relabel it.

## 4. Harness finding C — invariant-check cost grew with run length

The per-interval historical checkpoints show the harness CPU per cycle rising: 20 ms (first
10 minutes), 174 ms (4 h), 493 ms (8 h), 910 ms (12 h), and a plateau of ~985 ms from about 17 h,
when trades stopped closing. Wall time per cycle went from 2.05 s to 3.0 s, while the engine's own
timed cycle latency stayed flat (p95 143–205 ms).

The cause is the harness: `check_invariants` re-read the fills of **every** CLOSED execution on
every cycle, which is O(n²) over a run. As a result the run completed 32,702 cycles instead of
≈ 42,000 at a 2 s poll. This is not an engine defect, and it did not hide any violation (each
closed execution was checked many times).

## 5. What changed and what did not

**Changed (simulation harness, reporting and docs only):**
* `src/aseke_exec/soakgen.py` (new) holds the soak workload.
  * `MarketWalk` is the same seeded walk with the same RNG calls in the same order (a test compares
    it against the old inline loop).
  * `exit_levels`, the level policy `aseke.exec.soak-levels/2`:
    * The base offsets stop 500 / TP1 800 / TP2 1500 are unchanged whenever every below-reference
      level stays ≥ 50 % of the reference. For LONG that holds across the walk's whole range; for
      SHORT it holds when ref ≥ 3000.
    * Otherwise all three offsets of that signal are scaled by the single factor
      `0.5·ref / largest-below-offset` and floored to the **common tick**, the LCM of all
      instrument ticks (`sim_alpha` 0.1, `sim_beta` 0.05 → 0.1).
    * Geometry (LONG: stop < ref < TP1 < TP2; SHORT: TP2 < TP1 < ref < stop), the 0.5/0.5
      fractions and direction are preserved. Nothing is clamped to zero or to the entry, no
      direction flips, no signal is skipped, and there are no extra RNG draws.
    * Degenerate inputs raise `SoakWorkloadError`.
  * `validate_generated` runs the **production** strict decoder and parser
    (`load_event_json` + `parse_event`) and a per-venue tick check before signing and publishing.
* `src/aseke_exec/soak.py` (report format `aseke.exec.soak-report/3`):
  * Generator failures, including any unexpected `ArithmeticError`/`ValueError`/`KeyError`/
    `TypeError`, are counted, never published, and never counted as published.
  * TEST-ONLY `FUNDING_PROFILES` replace only the synthetic simulator starting wallets of the new
    root (`dataclasses.replace`):
    * `config` keeps the configured 1,000 USDT;
    * `funded-test-100k` gives 100,000 synthetic USDT per account;
    * `low-capital-test-50` gives 50 synthetic USDT per account.
  * Expected rejections are set per profile. `INSUFFICIENT_COLLATERAL` is expected only in
    `low-capital-test-50`.
  * Identity and report record the profile, the starting and configured balances, the config hash,
    an effective funding fingerprint, the seed, the source fingerprint and git head, and the
    effective account policies.
  * New report sections: `reconciliation`, `balances` (with ledger reconciliation),
    `coverage_by_quarter`, `workload_acceptance`, `invariant_check_ms`; checkpoints carry `wallets`.
  * The exit-quantity invariant is now incremental: each CLOSED execution is checked once when it
    closes, plus a **full** final pass that is de-duplicated against violations already counted.
* `src/aseke_exec/cli.py`:
  * new `soak --funding-profile`;
  * the soak exits 1 when `workload_acceptance.ok` is false (generator error, non-JOURNALED
    ingest, unexpected rejection, or failed reconciliation) and prints
    `SOAK WORKLOAD REVIEW REQUIRED`;
  * exit 5 for interrupted runs is unchanged.
* `src/aseke_exec/__init__.py`: `PHASE = "2C-simulation-only"`.
* `tests/test_soak_workload.py` (new, 96 tests). Docs: README, runbook, AGENTS.md, CLAUDE.md, and
  this report.

**Deliberately unchanged:**
* the signal contract and its `INVALID_NUMBER` checks (an external nonpositive-price signal is still
  quarantined, see the test);
* the engine, sizing, reservation and collateral logic;
* account policy: 100 USDT margin per trade, signal leverage (5x in the soak), isolated margin,
  adverse-entry tolerance, spread limit, collateral buffer, hard notional/margin limits, fees,
  instrument rules;
* IOC entry, SL/TP semantics, order protection, account ownership;
* HMAC verification and the strict profile (no fallback to unsigned);
* the runtime pin and `runtime_policy`;
* the single-writer topology;
* the simulator's rollback-journal mode;
* `config/*.toml`.

There is no automatic replenishment, unlimited credit, margin bypass, altered threshold or P&L
bias. The historical reports and roots are unchanged. `config/runtime-lock.json`
`evidence.extended_soak` stays `PENDING`.

## 6. New evidence (actual observations)

All runs used new, uniquely named roots under `runtime\`. Strict-signed runs used a fresh
TEST-ONLY key generated into the agent session's temporary scratch directory (outside the
repository, never printed, loaded into the process environment for each run only).

**Evidence classes, kept separate:**
* **(R24)** the original 24 h real-time run (§1–§4);
* **(A)** accelerated runs, which inject a simulated clock and are **not** real-time;
* **(S)** the short real-time smoke run;
* **(N)** the negative test.

Every Phase 2C run used the final reviewed source: source fingerprint
`3e013bfb47362b4ce06d15109d99a59f2c37f3288e30f4c7d768d67d76219d5b`, git head `9355dc2` plus the
working tree, config `config\sim-strict.toml` (hash `5f4f07b4…3f58da`), strict-signed, seed 7,
`trade_every_cycles` 8, holding 30 s, approved runtime 3.15.2 / SQLite 3.53.4.

### 6.1 (A) Funded accelerated, full historical length

Root `runtime\p2c-accel-funded-complete-20261118232407\`, profile `funded-test-100k` (100,000
synthetic USDT per account; TEST-ONLY, not real capital). 32,702 cycles, simulated poll 2 s
(65,404 simulated seconds), 6,326.7 s wall.

| Measure | Result |
|---|---|
| Status / exit | `COMPLETED` / **exit 0**, `workload_acceptance.ok = true` |
| Signals | generated 4,088 = published 4,088. **Generator errors 0**; 173 SHORT scaled. Ingest **JOURNALED 4,088**: 0 invalid; archive 4,088, quarantine 0, inbox 0 |
| Executions | 8,176 = 4,088 OPEN × 2: **CLOSED 4,746**, `OVERLAPPING_TRADE` 3,428 (expected), `OPEN_PROTECTED` 2. **`INSUFFICIENT_COLLATERAL` 0**; unexpected rejections: none; event rejections: none |
| Reconciliation | all 9 checks true. Journal orders 21,717 = simulator 21,717; fills 10,840 = 10,840; simulator open positions 2 = active executions 2 |
| Invariants / incidents / critical | **0** / 0 open / 0. The exit-quantity check covered all 4,746 CLOSED executions in-loop plus a full final pass |
| Protection | 4,748 measured entry→verified-stop intervals, max 0.001 s (injected clock; not real-time evidence) |
| Balances | alpha 100,000.00 → 88,581.25159776 (available 88,484.38), fees −1,373.71, realized P&L −10,045.04; beta 100,000.00 → 88,796.64573448 (available 88,699.78), fees −1,166.66, realized P&L −10,036.69. **Both ledgers reconcile exactly**; no other ledger entries (no replenishment) |
| Storage | journal WAL and simulator DELETE: `integrity_check ok`, 0 foreign-key violations; journal 0.40 → 49.9 MB, simulator 0.08 → 8.6 MB |
| Engine cycle latency | p50 61.6 ms, p95 273 ms, p99 515 ms, max 840 ms; first-10 mean 35.6 ms → **last-10 mean 173.8 ms** |
| Invariant check | first-10 1.35 ms → last-10 152.3 ms (p50 47.9 ms, max 243 ms) |
| Process | CPU 5,512 s (168.6 ms/cycle); working set 36.7 → 48.4 MB (max 48.6) |

**Coverage per quarter** (closed / overlapping refusals, identical for both accounts):

| Q1 0–8175 | Q2 8176–16350 | Q3 16351–24526 | Q4 24527–32701 |
|---|---|---|---|
| 594 / 428 | 593 / 429 | 589 / 433 | **597 / 424** (+1 open) |

Trading activity was sustained through all four quarters, including the low-price stretch where
the original run had stopped trading. That is the coverage the original 24 h run lacked.

**Investigated, not defects:**
* **The 2 `OPEN_PROTECTED` executions at the end** are `soak-032688`, published 14 cycles before
  the end with a 30 s (15-cycle) holding rule. Each has `stop_coverage VERIFIED`, a venue
  STOP_MARKET covering the full quantity (154 / 1,540 contracts at 2,645) and two TPs. This is the
  run-end state, not an orphan or an incident.
* **Realized P&L −10,04x per account** (versus −145 / −268 in 6.2) comes from the funded accounts
  continuing to trade through the walk's low-price region. There, ±900 / ±1,600 jumps gap through
  stops. 135 of 3,046 exit legs per account realized more than −100 USDT (worst −836.50 and
  −837.30) against a 100 USDT isolated margin. On a real isolated-margin venue such a position
  would be liquidated near the margin. **The simulator has no liquidation model and fills
  gap-through stops at the jumped price.** This is a known simulator limitation (§ Remaining
  limitations), not an execution or accounting fault. The stops were placed and verified, and the
  ledger reconciles. It says nothing about strategy profitability.
* **The latency trend.** The engine's own cycle time grew about 5× over this run while the
  journal grew to 49.9 MB under sustained trading. That growth was not visible in the original
  24 h run, because entries stopped after about 59 % of it. Its cause was **not investigated** in
  Phase 2C. At a 2 s real-time poll the observed p99 (515 ms) still leaves headroom, but this is a
  primary item to watch in the recommended funded 24 h run (§9).

### 6.2 (A) Original-capital comparison, corrected workload

Root `runtime\p2c-accel-config-final-20261118220300\`, profile `config` (1,000 synthetic USDT per
account), 32,702 cycles, simulated poll 2 s (65,404 simulated seconds), 4,661.9 s wall.

| Measure | Result |
|---|---|
| Status / exit | `COMPLETED` / **exit 1, by design** (`SOAK WORKLOAD REVIEW REQUIRED`: unexpected `INSUFFICIENT_COLLATERAL`) |
| Signals | generated 4,088 = published 4,088. Generator errors 0; 173 scaled. Ingest **JOURNALED 4,088**: 0 invalid, 0 quarantine files, archive 4,088, inbox 0 |
| Executions | 8,176 = 4,088 OPEN × 2 accounts: CLOSED 2,826, `OVERLAPPING_TRADE` 2,039 (expected), `INSUFFICIENT_COLLATERAL` 3,311 (**unexpected_review**) |
| Reconciliation | all 9 checks true. Journal orders 12,944 = simulator 12,944; fills 6,502 = 6,502; 0 open positions, 0 active executions |
| Invariants / incidents / critical | 0 / 0 / 0 |
| Protection | max entry→verified-stop 0.001 s (injected clock; not comparable with real time) |
| Balances | alpha 1,000.00 → 55.98395600 (fees −799.43, P&L −144.59); beta 1,000.00 → 44.78563721 (fees −687.00, P&L −268.22). Both ledgers reconcile exactly |
| Coverage per quarter (closed / refused) | Q1 594/428 OT and 594/428 OT; Q2 593/429 OT and 593/429 OT; Q3 alpha 225 closed + 635 IC, beta 227 + 632 IC; **Q4 0 closed, 1,022 IC per account** (OT = overlapping trade, IC = insufficient collateral) |
| Storage | journal WAL and simulator DELETE: `integrity_check ok`, 0 foreign-key violations |

**Interpretation.** With every signal valid, 1,000 USDT is still exhausted at the same point as in
the original run, so the historical collateral refusals were fee- and P&L-driven and were not
caused by the 90 invalid signals. The corrected harness reports this for review (exit 1) and does
not relabel it.

### 6.3 (S) Real-time strict-signed funded smoke

Root `runtime\p2c-smoke-funded-20261119011016\`, profile `funded-test-100k`, **real-time**,
`--duration-seconds 150 --poll-seconds 0.5`, run alone (no concurrent soak).

| Measure | Result |
|---|---|
| Status / exit | `COMPLETED` / **exit 0**, `workload_acceptance.ok = true`; elapsed 150.316 s wall |
| Cycles / signals | 280 cycles; 35 generated = 35 published = 35 JOURNALED; generator errors 0 |
| Executions | 70 = CLOSED 16, `OVERLAPPING_TRADE` 52 (expected), `OPEN_PROTECTED` 2 at the end; unexpected none |
| Reconciliation | all 9 checks true (orders 81 = 81, fills 46 = 46, positions 2 = active 2) |
| Invariants / incidents / critical | 0 / 0 / 0 |
| Protection | max entry→verified-stop **0.032 s** (18 measured) |
| Balances | alpha 100,000.00 → 100,027.50823142 (fees −4.69, P&L +32.20); beta 100,000.00 → 100,029.80350391 (fees −4.14, P&L +33.94); ledgers reconcile |
| Latency | p50 13.5 ms, p95 59.1 ms, p99 161.6 ms, max 168.1 ms; first-10 mean 33.0 ms vs last-10 mean 33.5 ms. Invariant check p50 1.4 ms |
| Resources | CPU 4.9 s; working set 36.9 → 37.4 MB; journal 0.40 → 4.50 MB |
| Storage | both stores `integrity_check ok`, 0 foreign-key violations (WAL / DELETE) |

Every quarter has closed executions for both accounts (2, 1, 1 and 4). At a 0.5 s poll with a
signal every 4 s and a 30 s holding rule, overlap refusals dominate by design.

### 6.4 (N) Low-capital negative test

Root `runtime\p2c-lowcap-final-20261118232413\`, profile `low-capital-test-50` (50 synthetic USDT),
400 accelerated cycles, 9.4 s wall, exit 0.

| Measure | Result |
|---|---|
| Signals | 50 generated = 50 published = 50 JOURNALED |
| Executions | 100 = 50 × 2, **all REJECTED `INSUFFICIENT_COLLATERAL`**, which is this profile's expected reason. Unexpected: none |
| Venue activity | **0 simulator orders, 0 fills, 0 positions**: no order reached the venue |
| Balances | 50.00 → 50.00 for both accounts; ledgers reconcile; fees 0 |
| Invariants / storage | 0 violations; both stores `integrity_check ok`, 0 foreign-key violations |

An earlier run of the same negative test on pre-review source
(`runtime\p2c-lowcap-20261118215632\`) gave identical counts; it is superseded.

### 6.5 Invariant-check cost after the fix: reduced, not eliminated

`invariant_check_ms` is measured per cycle:

| Run | first-10 mean | p50 | p95 | last-10 mean | max |
|---|---|---|---|---|---|
| 6.2 config, 32,702 cycles, 2,826 closed | 2.05 ms | 52.2 ms | 86.1 ms | **74.0 ms** | 166.6 ms |
| 6.1 funded, 32,702 cycles, 4,746 closed | 1.35 ms | 47.9 ms | 138.0 ms | **152.3 ms** | 243.5 ms |
| 6.3 real-time smoke, 280 cycles | 1.31 ms | 1.4 ms | 1.9 ms | 1.37 ms | 2.7 ms |

For comparison, the original harness reached about **985 ms** of CPU per cycle (§4), with quadratic
growth. The incremental check reads each closed execution's fills only once. However, every cycle
still selects the `id, filled_qty` rows of all CLOSED executions to find new ones, which is
**linear** in the number of closed executions: about 2.6–3.2 ms per 100 closed on this
workstation.

A funded 24 h real-time run at a 2 s poll (≈ 43,000 cycles, ≈ 6,000+ closed per account pair) would
by this estimate reach about 0.2 s per cycle by the end. That is bounded and visible in
the report, but **not constant**.

A constant-cost alternative is to check only executions that were active in the previous cycle
plus newly inserted ids. It is not implemented in Phase 2C (Remaining limitations).

### 6.6 Superseded and partial runs (not evidence)

| Root | Why not evidence |
|---|---|
| `runtime\p2c-accel-funded-20261118215650\`, `runtime\p2c-accel-config-20261118215651\` | started on pre-review source and stopped at ≈ 3,608 cycles for the review fixes; no report. One holds a hot `sim_exchange.db-journal`, left untouched |
| `runtime\p2c-accel-funded-final-20261118220259\` | stopped at cycle 26,122 (0 violations, 0 generator errors) on the operator's "stop" request; no report |
| `runtime\p2c-lowcap-20261118215632\` | pre-review source; superseded by 6.4 |

All of these roots are preserved as they are.

## 7. Tests and regression

| Check | Result |
|---|---|
| Baseline before changes (approved runtime) | 215 passed, 27/27 scenarios, pyflakes clean. Matches Phase 2B; no pre-existing deviation |
| `tests/test_soak_workload.py` | 96 passed (≈ 38 s) |
| Full suite after all changes | **311 passed** (215 existing + 96 new) in 232 s; scenario runner 27/27; pyflakes clean on `src tests scripts` |

The 27 scenarios are the same scenarios that `tests/test_scenarios.py` runs inside pytest, so they
are not extra evidence on top of the 311. The new tests cover:
* low and boundary prices (1000 … 98,205) × LONG/SHORT × ticks 0.1 / 0.05 / 0.5 (72 parametrised
  cases), all parsed by the production parser;
* preservation of the base offsets where they were valid;
* scaling rather than clamping, and degenerate inputs raising;
* LCM tick;
* the contract still rejecting the legacy SHORT (`INVALID_NUMBER`, and TP2 = 0);
* tick misalignment;
* RNG equivalence with the old loop;
* the seed-7 32,702-cycle historical replay (90 → 0) and five 40,000-cycle seeds;
* end-to-end: a low-price soak; a generator error counted and never published (CLI exit 1); the
  funded profile changing only wallets with reconciled ledgers and four-quarter coverage; the
  low-capital refusal; collateral never expected in a funded profile; an unknown profile refused
  before any file; a `config`-profile collateral refusal exiting 1;
* from the review follow-ups: quarter-label correctness for uneven spans, no double counting in the
  final pass, and ledger/reconciliation tamper detection;
* a strict-signed **external** nonpositive-price signal still quarantined `INVALID_NUMBER` with no
  execution.

Commands (repository root):
```powershell
.\.venv-exec\Scripts\python.exe -m pytest --basetemp $env:TEMP\aseke-pt tests\test_soak_workload.py
.\.venv-exec\Scripts\python.exe -m pytest --basetemp $env:TEMP\aseke-pt
.\scripts\aseke-exec.ps1 scenario run all
.\.venv-exec\Scripts\python.exe -m pyflakes src tests scripts
```
Use a short private `--basetemp`. The default pytest temp root on this workstation is locked.

## 8. Review (`ecc:code-reviewer`, static, over the Phase 2C diff)

The verdict was APPROVE: no CRITICAL or HIGH findings. The reviewer confirmed:
* the contract is not loosened;
* only wallets change;
* auth is unchanged and no key is printed;
* RNG order is identical;
* a generator error cannot reach `publish_event`;
* the ledger equation is correct, because starting wallets are not ledger rows.

| # | Severity | Finding | Outcome |
|---|---|---|---|
| 1 | MEDIUM | the final full invariant pass would re-count a violation already counted in-loop | **fixed**: the final pass is de-duplicated against seen violations; test added |
| 2 | MEDIUM | `coverage_by_quarter` cycle labels disagreed with the bucketing when `cycles % 4 ≠ 0` | **fixed**: labels use the exact inverse (`ceil`); test with spans 10/11/13/32702 |
| 3 | LOW | only `SoakWorkloadError` counted; another exception in signal construction would crash the soak | **fixed**: `ArithmeticError`/`ValueError`/`KeyError`/`TypeError` are also counted as generator errors |
| 4 | LOW | reconciliation equations assume the clean workload (HELD files stay in the inbox; uniform routing; no archive retry) | **accepted**. Any non-JOURNALED outcome already fails `workload_clean`; documented here |
| 5 | LOW | `simulator open positions <= active executions` is one-sided | **accepted**. The per-cycle invariants already flag missing coverage/exposure mismatches |
| 6 | LOW | a report without `workload_acceptance` exits 1 | accepted (fail-safe) |
| gaps | — | tests for incremental vs final pass, reconciliation false, coverage spans, ledger tamper, CLI exit 1 | added (see §7) |

Because fixes 1–3 changed the source, the two accelerated runs started before the review were
**stopped**: `runtime\p2c-accel-funded-20261118215650\` and
`runtime\p2c-accel-config-20261118215651\`, both at ≈ 3,608 cycles with 0 violations. Both were
re-run on the final source. The stopped roots have no report. They are left untouched, including a
hot `sim_exchange.db-journal`, and are **not** used as evidence. The low-capital negative test was
also re-run on the final source.

## 9. Is a further funded 24 h soak warranted?

**Yes, recommended before any unattended-simulation sign-off.** The reasons:
1. The original 24 h run executed no entries in its last ~40 %, so real-time entry and exit
   throughput is proven for only ~13.5 h of wall time.
2. The Phase 2C harness changes (incremental invariant check, new reconciliation) have real-time
   evidence only from a short smoke run.
3. Accelerated runs inject the clock and do not exercise wall-clock behaviour: sleep and timer
   granularity, sustained file-system and antivirus interaction, memory and WAL growth over 24 h of
   wall time.
4. Under sustained funded trading the engine's own cycle latency grew about 5×, from a 35.6 ms
   mean to a 173.8 ms mean (6.1), and the invariant check grew linearly (6.5). Neither trend could
   appear in the original run, because its trading stopped. Both must be reviewed over a real
   24 h run.

The run is **operator-only** and must not be launched by an agent. Use its own console and
follow the runbook preflight:
```powershell
cd C:\Users\16477\Projects\aseke-btc-exec
.\scripts\aseke-exec.ps1 runtime-info
$ts = Get-Date -Format yyyyMMddHHmmss; $rt = "runtime\soak-24h-funded-$ts"; $cfg = "config\sim-strict.toml"
if (-not (Test-Path secrets\soak-k1.key)) { .\scripts\aseke-exec.ps1 auth gen-key --out secrets\soak-k1.key }
$env:ASEKE_EXEC_HMAC_SIM_DEMO_K1 = (Get-Content -Raw secrets\soak-k1.key).Trim()
.\scripts\aseke-exec.ps1 --config $cfg --data-dir $rt auth check
.\scripts\aseke-exec.ps1 --config $cfg --data-dir $rt soak --duration-seconds 86400 --poll-seconds 2 --progress-every-seconds 300 --funding-profile funded-test-100k
"soak exit code: $LASTEXITCODE"
```
Don't click inside that console while it runs (Select mode). Accept the run only when all of these
hold:
* exit 0;
* `workload_acceptance.ok = true`;
* every `coverage_by_quarter` entry shows closed executions for both accounts and no
  `INSUFFICIENT_COLLATERAL`;
* `reconciliation.all_reconcile`, and every balance `ledger_reconciles`;
* storage ok;
* the runbook's resource and protection categories reviewed by the operator.

Only after that review and an explicit operator sign-off should anyone consider setting
`evidence.extended_soak`. It stays `PENDING` until then.

## 10. Bounded go / no-go

* **Phase 3 read-only exchange adapter work: GO, bounded.** That means public market data,
  instrument rules, and account and position *reads* behind the existing adapter interface. No
  credentials may be committed, and no order-capable endpoints. The simulation core's lifecycle,
  storage and signed ingestion have 24 h stability evidence and corrected-workload coverage
  evidence. Read-only work does not depend on the pending unattended sign-off.
* **Unattended simulation operation: NO-GO until** the attended funded 24 h run in §9 passes and
  the operator signs off.
* **Live order placement / real capital: NO-GO.** It is out of scope. It would need real-venue
  validation of fills, liquidation, funding, bracket semantics and the entry→stop gap (measured,
  not eliminated), plus a real publisher authentication design.

## Remaining limitations
* The funded profile proves sustained harness coverage, not profitability. All balances are
  synthetic, and the soak does not model funding, liquidation or exchange-specific fills.
* Corrected-workload coverage has bounded evidence only: accelerated (injected clock) plus a short
  real-time run. The original 24 h run remains the only long real-time evidence, with the workload
  limitations in §2–§4.
* The low-price policy changes 173 SHORT signals in the seed-7 replay (ref < 3000). 83 of them were
  previously valid but very wide (TP2 more than 50 % below the reference). This is documented,
  deterministic behaviour.
* **The simulator has no liquidation model.** Gap-through stops fill at the jumped price, so a
  single trade can lose several times its 100 USDT isolated margin (worst −837 in 6.1). A real
  isolated venue would liquidate earlier. Synthetic P&L is therefore not representative.
* **Engine cycle latency grows with history under sustained trading:** 35.6 → 173.8 ms
  (first-10 vs last-10 mean) over 32,702 funded accelerated cycles, as the journal reached
  49.9 MB. The cause is not investigated. It is acceptable at a 2 s poll in this evidence, but it
  is open.
* **Invariant-check cost still grows linearly** with the number of CLOSED executions. It went from
  2 ms to 74 ms per cycle over 32,702 cycles in 6.2. That is ~13× below the original quadratic
  harness, but not constant. The per-cycle scan of CLOSED rows remains, and making it O(active) is
  deferred. Review `invariant_check_ms` in any long run.
* Reconciliation equations assume the clean workload (review item 4). Entry→verified-stop exposure
  is measured, not eliminated.
* HMAC is shared-key authentication only. The Lab publisher is not integrated.
* Phase 2B runtime limitations still apply (docs/phase2b-acceptance-report.md#remaining-limitations).
