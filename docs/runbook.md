# Operator runbook — recovery, backup/restore, health, soak (simulation only)

All commands run from the repository root through `.\scripts\aseke-exec.ps1` (which uses
only the approved `.\.venv-exec\Scripts\python.exe -I`, never another Python). **Use a separate runtime root for every test, demo, soak or
restore** (`--data-dir runtime\<name>`, git-ignored). Never delete a runtime directory as
routine recovery, and never delete `*.db-wal` / `*.db-shm` sidecar files by hand.

## Runtime topology and the SQLite WAL-reset bug
* Per https://sqlite.org/wal.html ("WAL-reset bug") versions 3.7.0–3.51.2 are affected; fixed in
  3.51.3+, backports 3.44.6 and 3.50.7. Trigger: two or more connections in separate threads or
  processes writing or checkpointing the same WAL database at the same instant. Rare, but it
  can corrupt the database.
* **Phase 2B remediation:** the executor's runtime is the project-private python.org CPython
  3.15.2 (amd64) in `.exec-runtime\`, with the side-by-side venv `.venv-exec`; its **loaded**
  SQLite is 3.53.4 (source id `2026-07-24 19:02:57 bf7c7f30…`). Pinned in
  `config/runtime-lock.json`; provisioned/verified by `scripts\setup.ps1`. The old `.venv`
  (3.12.10 / SQLite 3.49.1, affected) is preserved for diagnosis only.
* **Runtime gate (`runtime_policy.py`)** reports three separate facts: `wal_reset_fix`
  (FIXED / AFFECTED / UNKNOWN from the parsed loaded version, incl. backports and release
  exclusions such as the withdrawn 3.52.0), `approved_build` (exact pinned Python + SQLite
  version + source id + interpreter inside this checkout), and `extended_soak` (evidence status
  from the lock file; `PENDING`). `operational` requires the first two. Every command that can
  create runtime files, write a database or process signals/controls refuses (exit 4) on a
  non-operational runtime **before** touching the runtime root — via the wrapper and via direct
  `python -m aseke_exec`. Help, `validate-config`, `runtime-info`, `auth gen-key|check`,
  `scenario list` and the read-only `status`/`health` stay usable (with a warning). There is no
  environment-variable bypass. A version check is not a universal safety guarantee.
* **Rollback of the environment** (e.g. using the old `.venv`) therefore cannot resume
  operation: the gate refuses. Databases are never downgraded automatically; schema v2 stays v2.
* Project mitigation (kept unchanged in Phase 2B; a patched library is no reason to remove it):
  - **Journal (WAL):** exactly one read-write connection at a time — the worker's. `cycle`,
    `reconcile`, `run`, `backup`, `init-runtime` take the worker lock. Operator commands
    (`pause`, `resume`, `account …`, `incident resolve`) apply directly only when they can take
    the lock; while a worker runs they write an atomic control request into `control\` that
    the worker applies. `status`/`health` open the journal **read-only** (`mode=ro`); a
    read-only connection cannot write or checkpoint (verified: checkpoint fails), but if it is
    the last connection to close it may leave empty `-wal`/`-shm` files behind; the next writer
    absorbs them.
  - **Simulator DB:** rollback-journal (`DELETE`) mode instead of WAL, because the harness
    (`sim set-market`, `sim fault`) legitimately writes it from other processes.
* Residual: **unattended operation is still not approved** until the extended operator soak
  (below) has been run and reviewed. Global Python / Lab's Python is never touched; do not
  replace system or global DLLs.
* Keep runtime roots on a **local disk** (WAL does not work over network filesystems).

## Moving an existing runtime root onto the patched runtime
Nothing is migrated automatically. For a root created on the old runtime (e.g. `var\`):
```powershell
# 0) stop that root's worker (Ctrl+C in its console) - do NOT kill it from elsewhere
# 1) back it up. The old interpreter is refused for `backup`, so use the approved runtime; backup
#    only READS the stores via the SQLite Online Backup API under both locks.
.\scripts\aseke-exec.ps1 --data-dir var backup --dest runtime\backup-var-<yyyymmdd>
# 2) restore into a NEW root and let the first cycle reconcile before any entry
.\scripts\aseke-exec.ps1 --data-dir runtime\var-restored-<yyyymmdd> restore --backup runtime\backup-var-<yyyymmdd>
.\scripts\aseke-exec.ps1 --data-dir runtime\var-restored-<yyyymmdd> cycle
.\scripts\aseke-exec.ps1 --data-dir runtime\var-restored-<yyyymmdd> health --json
```
`backup` opens the source stores read-only. In the source root it creates only the lock files, and
possibly empty `-wal`/`-shm` files left by the read-only connection. The original root is otherwise
left as it was (keep it; do not delete sidecars). The manifest's `journal_schema_version` is the
version the source journal records (1 for a Phase 1 root). A schema-v1 root is
migrated to v2 when the restored copy is first opened. Evidence that old-runtime stores restore,
migrate and reconcile correctly on the new runtime: `scripts\storage_compat.py`
(docs/phase2b-acceptance-report.md §5).

## Health
```powershell
.\scripts\aseke-exec.ps1 --data-dir runtime\demo health          # exit 0 healthy, 1 not
.\scripts\aseke-exec.ps1 --data-dir runtime\demo health --json
```
`process.running` is true only for a **resident worker** (`run`) with a fresh heartbeat and a
live PID; a one-shot `cycle` is reported as such. `lifecycle.healthy` additionally requires: no
open incidents, every open position `stop_coverage=VERIFIED` and `tp_coverage≠DEGRADED`, no
order with an unresolved outcome, fresh quotes, no stage errors, and a SQLite runtime not
affected by the WAL-reset bug. Per account: entry gate reasons (blocked / disabled /
degraded), planned, actual and reserved margin, last reconciliation.

## Recovery procedures
| Situation | What the worker does | Operator action |
|---|---|---|
| Worker crashed / killed | On restart: startup reconciliation of every account before new entries; unknown entries/closes resolved by client order id; exchange-side fills during downtime discovered; overdue time exits closed (`TIME_EXIT_OVERDUE`) | Restart `run`; check `health` |
| Second worker started | Refused (OS lock, exit 3); the lock file is never deleted | None |
| Stale lock file after a crash | Ignored: only the OS lock counts, never PID existence | None |
| `PROTECTION_FAILED` / `UNPROTECTED_EXPOSURE` | Account blocked; reduce-only emergency close retried each cycle; a covering stop placed when possible; incident open until flat | Investigate; after flat: `incident resolve <id> --note …`, then `account unblock <acct>` |
| TP degraded (`tp_coverage=DEGRADED`) | Position kept, stop verified; TP retried with bounded backoff (5 s doubling to 300 s); after 5 failures a `TP_PLACEMENT_PERSISTENT` incident; new entries on that account gated until restored | Investigate the venue/TP level |
| Signing key missing | Signed files HELD in the inbox (fail closed); existing positions still managed | Load the key env var; files are then ingested |
| Inbox/archive move fails | Event already journaled; alert `INBOX_MOVE_FAILED`; retried next scan as duplicate | Fix permissions |
| Journal busy / write fails | That stage stops for the cycle (nothing externally effective without a persisted intent); other stages and accounts continue | Investigate disk / other process |
| Malformed control request | Moved to `control\rejected\`; alert | Re-issue the command |
| Unowned venue exposure | Incident + entry block; never adopted | Manual review |

`control\` must be writable only by the operator account: a control request can pause,
resume, enable, unblock and resolve incidents (it cannot create trades). The producer/publisher
only needs write access to `inbox\`.

## Fresh runtime (preferred over any reset)
```powershell
.\scripts\aseke-exec.ps1 --data-dir runtime\fresh-20261117 init-runtime
```
`sim init --reset` is refused while a worker runs **and** whenever the journal already
references simulated orders or executions (a simulator-only reset would leave the journal
inconsistent). Use a new runtime root instead.

## Backup and restore (coordinated, quiesced)
```powershell
# 1) stop the worker (Ctrl+C in its console). backup refuses while it runs.
.\scripts\aseke-exec.ps1 --data-dir runtime\demo backup --dest runtime\backup-20261117
# 2) restore ONLY into a NEW, empty root
.\scripts\aseke-exec.ps1 --data-dir runtime\demo-restored restore --backup runtime\backup-20261117
# 3) first cycle in the restored root reconciles before any entry
.\scripts\aseke-exec.ps1 --data-dir runtime\demo-restored cycle
.\scripts\aseke-exec.ps1 --data-dir runtime\demo-restored health
```
Protocol: `backup` takes the worker lock **and** the simulator-harness lock (so neither the
worker nor `sim set-market/fault` can write), copies `journal.db` and `sim_exchange.db` with
the SQLite Online Backup API (consistent snapshots including committed WAL content — never a
raw copy of only the main file), copies `inbox/archive/quarantine/control`, and writes
`manifest.json` **last** (format, one generation id, config hash, schema version, SQLite
version, SHA-256 and size of each file, row counts). Two independent snapshots are only one
consistent generation because every writer was quiesced for the whole copy. `restore`
verifies the manifest and checksums, refuses a non-empty destination, refuses targets outside
the new root, never overwrites the current runtime, and records `restored-from.json`.
Restoring the simulator models restoring the venue too; with a real venue only the journal
would be restored and startup reconciliation would reveal any divergence.

## Soak
* Short real-time smoke, strict signed (Phase 2B: 180.4 s, 333 cycles):
  `.\scripts\aseke-exec.ps1 --config config\sim-strict.toml --data-dir runtime\soak-smoke-<ts> soak --duration-seconds 180 --poll-seconds 0.5 --progress-every-seconds 30`
* Accelerated deterministic stress (injected clock — **not** a real-time soak):
  `.\scripts\aseke-exec.ps1 --data-dir runtime\stress-1 soak --cycles 3000 --accelerated --poll-seconds 1`

### Extended operator soak (24 h) — operator-started, not run by any agent
Run it deliberately in its **own** PowerShell console at the repository root. Nothing is
scheduled, and nothing runs in the background. The soak is a **clean** run: no deliberate venue
faults and no restarts, which are exercised separately by tests and scenarios. Do not mix the two.

**Preflight** (each must succeed):
```powershell
cd C:\Users\16477\Projects\aseke-btc-exec
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Check   # "nothing changed"
.\scripts\aseke-exec.ps1 runtime-info        # runtime_policy.operational = true, wal_reset_fix = FIXED,
                                             # approved_build_id python.org-cpython-3.15.2-amd64+sqlite-3.53.4
$ts = Get-Date -Format yyyyMMddHHmmss
$rt = "runtime\soak-24h-$ts"                 # NEW root; soak refuses an existing/non-empty one
$cfg = "config\sim-strict.toml"              # authentication profile: strict signed ingestion
if (-not (Test-Path secrets\soak-k1.key)) { .\scripts\aseke-exec.ps1 auth gen-key --out secrets\soak-k1.key }  # never printed
$env:ASEKE_EXEC_HMAC_SIM_DEMO_K1 = (Get-Content -Raw secrets\soak-k1.key).Trim()   # this console only
.\scripts\aseke-exec.ps1 --config $cfg --data-dir $rt auth check   # sim-demo-k1 secret_available: true (creates nothing)
```
Keep the PC awake for the run. Changing power settings is your decision; no script changes them.

**Start** (foreground; Ctrl+C stops it cleanly):
```powershell
.\scripts\aseke-exec.ps1 --config $cfg --data-dir $rt soak --duration-seconds 86400 --poll-seconds 2 --progress-every-seconds 300
"soak exit code: $LASTEXITCODE"   # 0 completed+clean, 1 violations/storage problem, 5 interrupted (partial)
```

**Progress, from a second console.** These commands open read-only (`mode=ro`) connections
and never start a writer. Set `$rt` to the same root first:
```powershell
Get-Content "$rt\soak-progress.json"                       # latest snapshot (atomically replaced)
Get-Content "$rt\soak-checkpoints.jsonl" -Tail 3          # append-only history
.\scripts\aseke-exec.ps1 --config config\sim-strict.toml --data-dir $rt health --json
```
Do **not** run `cycle`, `run`, `backup`, `sim …` or any other writer against the soak root while it
runs. They are refused by the lock, but don't try.

**Evidence files** in `$rt`, never overwritten, because the root must be new:
`soak-identity.json` (runtime, source, config and auth identity, plus parameters, written at start),
`soak-progress.json`, `soak-checkpoints.jsonl`, `soak-report.json` (final), `alerts.jsonl`, and the
stores. The **duration** is established by `elapsed_wall_seconds`, which is measured wall time and is
separate from the injected simulation time (absent in real-time mode). The cumulative `cycles` and
the checkpoint timestamps confirm it. The **invariant history** comes from the cumulative
`cycles_checked` and `violations_total` in every checkpoint: invariants are checked after every cycle,
so a healthy final snapshot alone does not prove the whole run was healthy. A Ctrl+C or crash leaves
`status = INTERRUPTED` (or no report) and exit code 5. That is **partial evidence, never a
completed soak**.

**Final evidence collection** (after the run ends):
```powershell
Get-Content "$rt\soak-report.json"
.\scripts\aseke-exec.ps1 --config config\sim-strict.toml --data-dir $rt health --json
.\scripts\aseke-exec.ps1 --config config\sim-strict.toml --data-dir $rt backup --dest "runtime\soak-24h-$ts-backup"
```
The report already contains `PRAGMA integrity_check` and `foreign_key_check` for both stores. The
checks run after the engine's writer connection has closed, while the soak still holds the worker
lock, so no other writer can be active. The backup adds a manifest with checksums and row counts.

**Acceptance categories.** Defined before the run. These are project test criteria, not
real-exchange guarantees:
| Category | Criterion |
|---|---|
| Completion | `status = COMPLETED`, `elapsed_wall_seconds` ≥ 86400, checkpoints continuous (no gaps much larger than the progress interval) |
| Exposure | zero duplicate or reversed exposure; zero `invariant_violations_total` |
| Persistence | `integrity_check = ok` and 0 foreign-key violations for both stores; no lost executed exit quantities (exit qty = entry qty for every CLOSED execution) |
| Incidents | no unresolved unexpected CRITICAL alerts or incidents (`critical_alerts`, `open_incidents`) |
| Rejections | every rejection classified; only the documented expected `OVERLAPPING_TRADE`, or explained |
| Protection | review `protection.max_unprotected_seconds` and its distribution; the entry→verified-stop interval is **measured, not eliminated** |
| Resources | review latency p50/p95/p99/max, first-10 vs last-10 means, working set first/last/max, journal growth. No universal limit is set; judge the trends |

Deliberate fault or restart exercises run in **separate** roots (for example the crash-point
recovery demo in docs/phase2b-acceptance-report.md) and must never be reported as the clean soak.
The soak validates neither funding nor liquidation behaviour.

Invariants checked every cycle: no exposure without an active execution, no reversal, one
  active execution per account/instrument, a venue stop covering every protected position,
  `stop_coverage=VERIFIED` only when the venue holds a covering stop, at most one live stop,
  working TP quantity ≤ position, CLOSING never silently uncovered, exit qty = entry qty
  for every closed execution, no orphan working orders.
