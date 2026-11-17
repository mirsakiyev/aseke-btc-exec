# Phase 2B acceptance report — project-isolated SQLite remediation

**Date:** 2026-11-16 · **Workstation:** Windows 11 Pro 10.0.26300 (x64) · **Source:** HEAD
`1aee5f1` + uncommitted Phase 2B working tree · **Mode:** simulation only.

> **Verdict:** Patched runtime verified; eligible to begin an isolated operator-controlled
> extended simulation soak. Extended-soak acceptance pending. No real-exchange/live-trading
> approval. Unattended operation is **not** approved: the 24-hour soak has **not** run.

## 1. Runtime before / after

| | Before (old `.venv`, preserved) | After (approved `.venv-exec`) |
|---|---|---|
| Executable | `…\aseke-btc-exec\.venv\Scripts\python.exe` | `…\aseke-btc-exec\.venv-exec\Scripts\python.exe` |
| Base prefix | `C:\Users\16477\AppData\Local\Programs\Python\Python312` (global, untouched) | `…\aseke-btc-exec\.exec-runtime\cpython-3.15.2-amd64` (project-private) |
| Python | 3.12.10 (tags/v3.12.10:0cc8128, MSC v.1943, 64-bit AMD64) | 3.15.2 (tags/v3.15.2:5e58eaa, MSC v.1951, 64-bit AMD64, not free-threaded) |
| Loaded SQLite | 3.49.1 | 3.53.4 |
| `sqlite_source_id()` | `2025-02-18 13:38:58 873d4e274b4988d260ba8354a9718324a1c26187a4ab4c1cc0227c03d0f10e70` | `2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc` |
| `_sqlite3` module | `…\Python312\DLLs\_sqlite3.pyd` | `…\.exec-runtime\cpython-3.15.2-amd64\DLLs\_sqlite3.pyd` |
| WAL-reset fix | AFFECTED | FIXED |
| Runtime policy | `operational: false` (operational commands refused, exit 4) | `operational: true`, `approved_build: true`, `extended_soak: PENDING` |

Values come from an in-memory probe (`SELECT sqlite_version(), sqlite_source_id()`) run in each
interpreter with `-I`, and from `runtime-info`. No runtime database was opened for this.
Dependencies in `.venv-exec`: pytest 9.2.0, jsonschema 4.26.0, pyflakes 4.1.0 (+ transitive, 13
pinned wheels with hashes in `requirements/dev-cp315-win_amd64.lock`). Executor runtime: stdlib only.

## 2. Runtime selection and provenance

**Upstream status (rechecked 2026-11-16).** Sources: https://sqlite.org/wal.html, last updated
2026-08-25, and https://sqlite.org/news.html. The WAL-reset bug is present in 3.7.0–3.51.2 and
fixed in 3.51.3 and later. Backports exist for 3.44.6 and 3.50.7. 3.52.0 was **withdrawn**
(compatibility) and re-released as 3.53.0, which includes the fix. 3.53.4 (2026-07-24) is a
maintenance patch, described upstream as "perhaps … the last patch release for 3.53". 3.54.0 has
a 32-bit-only heap overflow that 3.54.1 fixes. The newest release is 3.54.1 (2026-10-16).

**Why Python 3.15 and not 3.12.** The spec prefers 3.12, but that wasn't possible:
- python.org's Windows install-manager index (`index-windows.json`) lists 3.12.10 as the last
  Windows build for 3.12, which is now in security-only maintenance. That build ships SQLite
  3.49.1, which is affected.
- The latest 3.13 (3.13.17) and 3.14 (3.14.10) Windows builds both ship SQLite **3.50.4**,
  according to their python.org SPDX SBOMs. That is still affected, because the 3.50 backport
  is 3.50.7.
- **3.15.2** is the only python.org Windows build whose bundled SQLite (3.53.4.0 per its SBOM)
  contains the fix. Python 3.15 is a stable bugfix-phase release, first released 2026-10-09,
  with a scheduled end of life of 2031-10 (devguide.python.org/versions). It is neither a
  prerelease nor a free-threaded build.
- A python-build-standalone 3.12.16 archive (20261110) is present in
  `.exec-runtime\downloads\20261110` from an earlier exploratory attempt. It was **not selected**,
  and nothing installs or uses it. The crashed session recorded no reason for dropping it. The
  selected route needs no third-party tool (uv or `gh` attestation) and is Authenticode-signed
  by the publisher.

**Compatibility.** Code keeps `requires-python = ">=3.12"` and is stdlib-only. The full
regression below passes unchanged on 3.15.2, and the baseline still passes on 3.12.10. No
3.13+-only language features were introduced.

**Pinned build** (`config/runtime-lock.json`, `approved_build_id
python.org-cpython-3.15.2-amd64+sqlite-3.53.4`):
- artifact `python-3.15.2-amd64.zip`, 37 272 560 bytes, SHA-256
  `d6da9c33a32cf111e1d52b91392c83ea6a808545b646e0557f66cb6d9ee76785`
- source `https://www.python.org/ftp/python/3.15.2/python-3.15.2-amd64.zip`
- **Verification method** (`scripts\setup.ps1`, every run):
  1. the pinned digest in source control;
  2. the publisher's digest from python.org's install-manager index (entry `pythoncore-3.15-64`)
     when online;
  3. a **Windows Authenticode** signature check of every `.exe`/`.dll`/`.pyd` (signer
     `CN=Python Software Foundation`). The only allowed unsigned prefix is pip's vendored
     distlib launchers.
  A locally computed hash alone is not treated as authentication.
- The staged interpreter is probed (Python version, implementation, 64-bit, not free-threaded,
  loaded SQLite version **and** source id) before being atomically renamed into place.
- `.venv-exec` is created at its final path, wheels only, with `pip --require-hashes`. The
  marker `.venv-exec\aseke-venv.json` is written last, after `runtime-info` reports
  `operational`. The wrapper refuses a venv without the marker.

Binaries, venvs, caches and downloads live under the git-ignored `.exec-runtime/` and
`.venv-exec/`. The lock file, dependency lock and scripts are in source control.

## 3. Interpreter selection and runtime gate

- **One canonical launcher:** `scripts\aseke-exec.ps1`. It runs `.venv-exec\Scripts\python.exe -I
  -m aseke_exec`, resolves paths relative to the repository, handles spaces, propagates exit
  codes, needs no activation, and **never falls back**: a missing or unverified venv exits 4.
  `demo.ps1` calls it; `setup.ps1` provisions it.
- **Gate in Python** (`runtime_policy.py`, `cli._runtime_gate`): every command except help,
  `validate-config`, `runtime-info`, `auth gen-key|check`, `scenario list` and the read-only
  `status`/`health` refuses (exit 4) on a non-operational runtime **before** it creates runtime
  files, opens a database for writing, or processes signals or controls. This also applies to
  direct `python -m aseke_exec`.
- **Library entry points refuse too:** writable `Journal`, `SimExchange` (added after review,
  §9), `run_worker` and `run_soak` call `require_operational` before creating any file.
- **Observed:** `.venv\Scripts\python.exe -m aseke_exec --data-dir <new> init-runtime` → exit 4
  and no directory created. Library construction of `SimExchange` on the old venv is refused.
  `runtime-info` on the old venv still works (diagnostic).
- **Three facts, kept separate:** `wal_reset_fix` comes from the parsed numeric loaded version,
  `approved_build` is an exact pin match, and `extended_soak` is the lock-file evidence status.
  There is no environment-variable bypass. A running worker is never terminated by a diagnostic.
- **Entry paths inventoried:** the wrapper, `setup.ps1`, `demo.ps1`, direct module entry, the
  crash-test subprocesses (`sys.executable`, so the approved interpreter when pytest runs under
  `.venv-exec`), `storage_compat.py` (explicit `--python` for the old-runtime builder; the
  verifier must be operational), and the soak (in-process). Source code starts no subprocesses.
  No bare `python`/`py` or hard-coded old `.venv` path remains in routine commands.

## 4. Baseline reproduction (old runtime)
HEAD `1aee5f1` was exported with `git archive` into a scratch directory (no Git writes) and run
with `.venv` (3.12.10 / SQLite 3.49.1) using a private `--basetemp`. Result: **149 passed**,
**27/27 scenarios**, pyflakes clean. This matches the Phase 2A report.

## 5. Storage compatibility across the runtime change
`scripts\storage_compat.py build` ran with the **old** interpreter on the HEAD export (plus a
Phase 1 `9b4501f` export for schema v1). It produced isolated fixtures in a scratch directory:
- **A** (compat profile): a protected multi-TP position with a deadline, TP1 filled, a
  close-before-open tombstone, and a STOP intent left `INTENT` by a crash point (unknown order).
- **B** (strict profile): a signed protected position, a signed tombstone, and a signed OPEN
  HELD because the key was absent.
- **C**: a real schema-v1 root.

A and B were quiesced and backed up with the manifest-backed SQLite Online Backup. `verify` ran
with the **new** interpreter. It restored A and B into new roots (C was copied with the SQLite
API), ran reconciling cycles, and checked:

| Check | A | B | C |
|---|---|---|---|
| `integrity_check` / `foreign_key_check`, backup or copy (journal + simulator) | ok / 0 | ok / 0 | ok / 0 |
| same checks after restore + reconcile | ok / 0 | ok / 0 | ok / 0 |
| semantic problems¹ | none | none | none |
| specifics | unknown STOP intent resolved (`NOT_ACCEPTED`) in 2 cycles; no ghost OPEN | HELD signed event ingested after restore with the key | schema 1 → 2 |

¹ Event order preserved; no events, orders, fills or executions lost; entry accounting (qty,
price, fees, margin) unchanged; deadlines unchanged; executed TP quantities never decrease;
exit-plan versions never go backwards; tombstones kept; no INTENT orders left unresolved;
venue quantity equals remaining quantity; nothing marked protected without a verified stop.
Result: **PASS**.

**Defect found and fixed.** `backup` labelled every manifest with the code's
`SCHEMA_VERSION` (2), even for a v1 source. It now records the version the copied journal
itself records (`ops._recorded_schema`). Checked against the real v1 fixture: the CLI `backup`
manifest says `journal_schema_version: 1`, the source stays v1 (backup never migrates it), and
the restored copy migrates to 2 with `integrity_check ok`. New regression test
`test_backup_manifest_records_the_source_schema_not_the_code_schema`.

Integrity checks don't prove correct accounting or repair earlier corruption, which is why the
semantic checks above exist. The operator procedure for moving an existing root is in the
runbook ("Moving an existing runtime root onto the patched runtime"). Nothing was migrated
silently, and `var\` was not opened.

## 6. Regression on the approved runtime
Final run, after the review fixes:

| Suite | Result |
|---|---|
| pytest (`.venv-exec`, `--basetemp %TEMP%\ak`) | **215 passed**: the 149 Phase 2A tests unchanged, 55 runtime-policy, 10 setup/wrapper, 1 new backup-manifest test |
| scenarios (`aseke-exec.ps1 scenario run all`) | **27/27**. These are the same 27 scenarios that `tests/test_scenarios.py` runs inside pytest, so they are overlap, not additional evidence |
| pyflakes `src tests scripts` | clean |
| `setup.ps1 -Check` | "already provisioned and verified … nothing changed" |

The focused tests cover:
- interpreter identity, which must agree between the candidate, the effective interpreter and
  subprocesses;
- refusal of a missing or unapproved runtime before any runtime file exists (CLI and library
  entry points);
- no fallback when PATH contains poisoned `python`, `py` or `python3` shims;
- an interrupted setup (`-TestFailAfter after-venv`) followed by a repeat setup, which keeps the
  old `.venv` and existing `var\` data and sidecars byte-identical, leaves no staging leftovers,
  and does not reinstall on the third run;
- the wrapper working from another directory and from paths with spaces;
- `--data-dir` surviving the wrapper (`init-runtime`/`publish`/`cycle`/`status`/`backup`/`restore`);
- version classification: affected, fixed, backports, malformed or unknown versions, and
  exclusions for 3.52.0 and 3.54.0.

Crash/restart, strict HMAC, migration, backup/restore, process-lock, TP-coverage and hard-limit
tests are unchanged and pass.

**Setup defects found and fixed this phase** (both in `scripts\setup.ps1`):
1. **Module loading.** Windows PowerShell 5.1 started from PowerShell 7 inherits pwsh's
   `PSModulePath`, so `Get-FileHash` and `Expand-Archive` resolved to the incompatible 7.x
   modules and failed. Setup now imports the inbox Utility, Security and Archive modules from
   `$PSHOME` and leaves `PSModulePath` untouched. Covered by
   `test_setup_check_survives_psmodulepath_inherited_from_pwsh`.
2. **Path length.** With `LongPathsEnabled = 0`, a deep checkout path made `Expand-Archive`
   fail midway with a misleading cleanup error. Setup now checks archive path lengths up front
   and fails clearly, and the staging directory name is shorter.

**Test-environment notes.** pytest's default temp root
(`%TEMP%\pytest-of-16477\pytest-current`) is locked on this workstation, so its end-of-session
cleanup raises `PermissionError` after the tests have run. It was left alone; use a private
short `--basetemp`. Tests and scenarios ran offline.

## 7. Signed demo, recovery demo, smoke soak
The demos used new roots `runtime\p2b-*-20261116210206`. Each used a fresh key generated into
the session scratch directory, outside the repository. It was passed only via a process-scoped
environment variable. Searching all output for the key's first 20 characters found 0 matches.
The user's keys were not touched.
- **Strict signed demo** (`config\sim-strict.toml`): `auth check` showed `secret_available:
  true`. The signed OPEN was ingested and both accounts protected. The unsigned OPEN was
  rejected with `SIGNAL_AUTH_REJECTED AUTH_REQUIRED`. Price 66100 closed both accounts
  (TAKE_PROFIT).
- **Crash recovery:** `ASEKE_EXEC_CRASHPOINT=after_ack:ENTRY` made the cycle exit 97 after the
  venue filled the entry and before the local acknowledgement. The restart cycle reconciled
  both accounts to `OPEN_PROTECTED` (stop VERIFIED, TP COMPLETE) with no unresolved orders. The
  venue holds exactly 7 and 75 contracts with 2 fills, so there was no duplicate entry. Both
  stores: integrity ok, 0 FK violations.
- **Backup → restore → reconcile** of the recovered root into a new root: integrity ok, both
  positions `OPEN_PROTECTED`.
- `health` exits 1 in these demos only because one-shot cycles are not a resident worker
  (documented behaviour).

**Real-time smoke soak**: `--config config\sim-strict.toml --data-dir
runtime\p2b-smoke-20261116210247 soak --duration-seconds 180 --poll-seconds 0.5`, strict signed,
clean workload. It ran before the §9 review fixes, which change only gating and an error path,
not the soak workload.

| Measure | Phase 2B (3.15.2 / 3.53.4, strict signed) | Phase 2A smoke (3.12.10 / 3.49.1) |
|---|---|---|
| status / wall time | COMPLETED / 180.4 s | completed / 90.2 s |
| cycles (all checked for invariants) | 333 | 168 |
| signals published (all journaled) | 42 | 21 |
| executions CLOSED / REJECTED | 18 / 66 | 4 closed, 2 open / 36 |
| rejections | all `OVERLAPPING_TRADE` (expected by workload design) | same |
| invariant violations / incidents / critical alerts | 0 / 0 / 0 | 0 / 0 / – |
| max measured entry→verified-stop interval | 0.034 s (18 measured) | – |
| cycle latency p50 / p95 / p99 / max | 17.9 / 55.5 / 176.4 / 183.8 ms; first-10 mean 35.1, last-10 mean 25.3 | – |
| CPU | 7.5 s (22.5 ms/cycle) | – |
| working set first / last / max | 36.4 / 31.2 / 36.9 MB | – |
| journal / simulator size | 0.40 → 4.60 MB / 0.08 → 0.12 MB | – |
| storage at end | journal (WAL) and simulator (DELETE): integrity ok, 0 FK violations | – |

Different durations, authentication profiles and runtimes mean this is **not** a controlled
performance comparison. A 3-minute smoke proves nothing about a 24-hour run.

## 8. Requirement → evidence matrix
| Requirement | Evidence | Status |
|---|---|---|
| Worker running before env changes? | process scan: no `aseke_exec` process | none running; side-by-side install |
| Old runtime recorded, baseline reproduced | §1, §4 | done (149 / 27 / clean) |
| Patched SQLite actually **loaded** | in-memory probe + `runtime-info` in `.venv-exec` | 3.53.4, FIXED |
| Supported, non-withdrawn release; provenance verified | §2 | done |
| Project-private, idempotent, pinned setup; old `.venv` kept | `setup.ps1`, lock files, setup tests, `-Check` | done |
| No global Python/PATH/registry/launcher changes | setup design + poisoned-PATH test; nothing written outside the checkout | done (see §10 limits) |
| Canonical launcher, no fallback, spaces, cwd, exit codes | wrapper tests | done |
| Gate on direct entry before DB/runtime files | gate tests + old-venv observation | done |
| Single writer, rollback-mode simulator, locks unchanged | code unchanged; regression | done |
| Backup/restore and migration across runtimes, integrity + semantics | §5 | PASS |
| Full regression, lint | §6 | 215 / 27 / clean |
| Signed demo + recovery demo on new roots | §7 | done |
| Bounded real-time smoke | §7 | COMPLETED, 0 violations |
| Operator 24 h soak procedure + acceptance categories | docs/runbook.md "Extended operator soak" | documented, **not run** |
| ECC review | §9 | done; findings fixed or accepted |

## 9. Review (`ecc:code-reviewer`, static, over the Phase 2B diff)
The reviewer found no critical or high issues. The gate's diagnostic set, version
classification, setup ordering and idempotence, secret handling, and unchanged trading
semantics were all confirmed.

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | MEDIUM | `SimExchange` opened its writable store without `require_operational`. Not reachable via the CLI, but library use bypassed the gate | **Fixed** (`sim.py`). The test was extended in `test_library_entries_refuse_too`, and the old venv was observed being refused |
| 2 | LOW | The approval check doesn't verify that `aseke_exec` itself is imported from this checkout | Accepted. The policy protects the interpreter and SQLite build, which are checked exactly |
| 3 | LOW | Re-runs re-check Authenticode and the probe but not per-file content hashes of an installed interpreter | Accepted. The archive is hash-pinned and the machine is local. Recorded as a limitation |
| 4 | LOW | `_sqlite_copy` leaked the source connection if opening the destination failed | **Fixed** (`ops.py`, nested `finally`) |

The full regression was rerun after the fixes (§6).

## 10. Preservation statement and its limits
- **Global Python** (`…\Programs\Python\Python312`) and `py` launcher: not modified. Setup writes
  only inside the checkout (`.exec-runtime\`, `.venv-exec\`) and sets only process-scoped
  environment variables.
- **aseke-btc-lab:** not opened, read or modified.
- **Old `.venv`:** kept at its path; only used read-only (probe, baseline run against an export,
  refusal checks).
- **User runtime data:** `var\` and the pre-existing `runtime\*` roots (from Phase 2A) were not
  opened or modified. All evidence used new `runtime\p2b-*` roots or scratch directories.
- **Limits:** this statement rests on how the scripts are designed and on the commands this
  session ran. No filesystem or registry audit trail was captured. Provisioning happened in an
  earlier, crashed session whose own commands were not fully observed in this one; the result
  was re-verified (signatures and digests on each `setup.ps1 -Check`/run, marker, probe).

## Remaining limitations
- **Extended soak not run.** `evidence.extended_soak = PENDING`; unattended operation is not
  approved.
- **SQLite 3.53 line maintenance:** upstream suggests 3.53.4 may be the last patch for 3.53.
  Moving to a newer build (for example a CPython release bundling 3.54.1+) needs a deliberate
  lock-file update and a full re-run of this report's checks.
- **Python 3.15** is a newer minor than the Phase 1/2A runtime. Compatibility is shown by this
  regression suite only.
- **Installed-interpreter content hashes** aren't re-verified on each run (review finding 3).
- **Authentication:** HMAC shared-key only; the soak uses the local test key `sim-demo-k1`. No
  Lab publisher exists.
- **No validation** of funding, liquidation or any real-exchange behaviour. The
  entry→verified-stop interval is measured, not eliminated. Separate (non-bracket) protective
  orders.
- **Windows path limits:** checkout paths longer than about 120 characters fail setup (clearly).
- **pytest default temp root** on this workstation has a locked `pytest-current` entry, which
  is an environment issue.
- **Network:** official documentation and the SBOM/index pages were fetched this session.
  Provisioning downloads happened in the earlier session. Tests, scenarios, demos and the smoke
  soak ran offline.
