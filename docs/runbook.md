# Operator runbook — recovery, backup/restore, health, soak (simulation only)

All commands run from the repository root through `.\scripts\aseke-exec.ps1` (which uses
`.\.venv\Scripts\python.exe`). **Use a separate runtime root for every test, demo, soak or
restore** (`--data-dir runtime\<name>`, git-ignored). Never delete a runtime directory as
routine recovery, and never delete `*.db-wal` / `*.db-shm` sidecar files by hand.

## Runtime topology and the SQLite WAL-reset bug
* Embedded SQLite on this workstation: **3.49.1** (`runtime-info`). Per
  https://sqlite.org/wal.html ("WAL-reset bug") versions 3.7.0–3.51.2 are affected; fixed in
  3.51.3+, backports 3.44.6 and 3.50.7. Trigger: two or more connections in separate threads or
  processes writing or checkpointing the same WAL database at the same instant. Rare, but it
  can corrupt the database. The upstream page names no mitigation other than upgrading.
* Project mitigation (in place, Phase 2A):
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
* Residual: the runtime library itself is still affected. **Unattended operation is not
  certified** until a project-isolated runtime with SQLite ≥ 3.51.3 (or 3.50.7 / 3.44.6) is in
  place. Remediation plan (operator decision, not done automatically): install a separate
  Python whose bundled `sqlite3` is a fixed version into a project-local path, recreate
  `.venv` from it, verify with `runtime-info` (`wal_reset_bug: FIXED`), and leave the global
  Python used by aseke-btc-lab untouched. Do not replace system or global DLLs.
* Keep runtime roots on a **local disk** (WAL does not work over network filesystems).

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
* Short real-time smoke (measured in Phase 2A: 90.4 s):
  `.\scripts\aseke-exec.ps1 --data-dir runtime\soak-smoke-1 soak --duration-seconds 90`
* Accelerated deterministic stress (injected clock — **not** a real-time soak):
  `.\scripts\aseke-exec.ps1 --data-dir runtime\stress-1 soak --cycles 3000 --accelerated --poll-seconds 1`
* **Optional extended operator soak (not run in Phase 2A; run deliberately, in its own console):**
  ```powershell
  .\scripts\aseke-exec.ps1 --data-dir runtime\soak-24h soak --duration-seconds 86400 --poll-seconds 2
  ```
  It writes `runtime\soak-24h\soak-report.json` (elapsed wall time, cycles, latency
  p50/p95/max, first/last-10 mean, working set first/last/max, journal and simulator size,
  states, alerts, invariant violations). Inspect `health --json` meanwhile from another
  console. The report records measurements; it does not certify readiness by itself.
  Invariants checked every cycle: no exposure without an active execution, no reversal, one
  active execution per account/instrument, a venue stop covering every protected position,
  `stop_coverage=VERIFIED` only when the venue holds a covering stop, at most one live stop,
  working TP quantity ≤ position, CLOSING never silently uncovered, exit qty = entry qty
  for every closed execution, no orphan working orders.
