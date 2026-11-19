# ASEKE BTC Exec — Phase 2C (simulation only)

ASEKE BTC **Lab** decides which trading opportunities to authorize. ASEKE BTC **Exec**
(this repository) receives those authorizations, validates them, sizes positions from its
own per-account configuration, executes through adapters, manages protection and exits,
and reconciles state.

> **Simulation only.** No real exchange orders, no API credentials, no authenticated
> exchange accounts, no fund transfers. `mode = "live"` and `LIVE` events are refused before
> any order-capable activity. The synthetic venues `sim_alpha` / `sim_beta` are not models of
> any real exchange. Nothing here validates real-exchange behaviour or implies profitability.
> This is **not** live-trading approval.

## What it does
- Strict versioned JSON signal contract (`aseke.exec.signal/1`):
  [docs/signal-contract.md](docs/signal-contract.md), [schema](schemas/signal.v1.schema.json),
  [examples](examples/signals/).
- Optional **authenticated envelope** (`aseke.exec.envelope/1`, HMAC-SHA-256, key scopes,
  revocation, fail-closed): [docs/signal-envelope.md](docs/signal-envelope.md).
- Atomic file inbox → durable SQLite journal (single writer) → per-account lifecycle:
  validation → sizing/reservation → IOC entry → verified exchange-held protection
  (stop coverage and TP coverage tracked separately) → TP/SL/time/Lab exits → reconciliation,
  incidents, alerts.
- Persistent deterministic simulator (own SQLite file) with fault injection.
- Operator tooling: `health`, `runtime-info`, isolated `init-runtime`, quiesced
  `backup`/`restore` into new roots, bounded `soak`. See [docs/runbook.md](docs/runbook.md).

Architecture: [docs/architecture.md](docs/architecture.md). Acceptance evidence:
[Phase 1](docs/acceptance-report.md), [Phase 2A](docs/phase2a-acceptance-report.md),
[Phase 2B — patched runtime](docs/phase2b-acceptance-report.md),
[Phase 2C — soak workload corrections and closeout](docs/phase2c-acceptance-report.md) (current verdict).
Future real adapters: [docs/future-adapter-checklist.md](docs/future-adapter-checklist.md).
Rules for contributors/agents: [AGENTS.md](AGENTS.md).

> **Runtime note:** the executor runs only on the **approved project-private runtime** pinned in
> [config/runtime-lock.json](config/runtime-lock.json): python.org CPython 3.15.2 (amd64) whose
> loaded SQLite is 3.53.4 (contains the upstream WAL-reset fix). Operational commands refuse any
> other interpreter (exit 4), including the old `.venv` (Python 3.12.10 / SQLite 3.49.1, affected),
> which is kept only for diagnosis. The single-writer topology stays in place. One clean
> strict-signed 24 h simulation soak has completed (Phase 2C reviews its workload limitations);
> `evidence.extended_soak` stays **pending** operator sign-off, and unattended or live operation
> is not approved. Check with `.\scripts\aseke-exec.ps1 runtime-info`.

## Windows setup (PowerShell, repository root)
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1          # provision / verify (idempotent)
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Check   # verify only, changes nothing
```
`setup.ps1` downloads the pinned python.org zip into `.exec-runtime\` (git-ignored), verifies the
pinned SHA-256, the python.org index digest (when online) and the PSF Authenticode signature of
every binary, probes the staged interpreter's loaded SQLite, creates `.venv-exec` at its final
path from hash-locked wheels, and writes its verification marker last. It never touches the old
`.venv`, `var\`, runtime roots, global Python, PATH, the registry or the `py` launcher.

Every command goes through `.\scripts\aseke-exec.ps1` (= `.\.venv-exec\Scripts\python.exe -I -m
aseke_exec`; no fallback to any other Python). Use `--data-dir runtime\<name>` to give every
demo/test/soak its **own runtime root** (git-ignored). Without `--data-dir` the config's
`data_dir` (`var\`) is used.

## Demonstration (unsigned compatibility profile, isolated root)
```powershell
$rt = "runtime\demo-" + (Get-Date -Format yyyyMMddHHmmss)
.\scripts\aseke-exec.ps1 --data-dir $rt init-runtime
.\scripts\aseke-exec.ps1 --data-dir $rt publish-example long --trade-id demo-long-1
.\scripts\aseke-exec.ps1 --data-dir $rt cycle
.\scripts\aseke-exec.ps1 --data-dir $rt status          # both accounts OPEN_PROTECTED, stop/tp coverage
.\scripts\aseke-exec.ps1 --data-dir $rt sim set-market --price 66100
.\scripts\aseke-exec.ps1 --data-dir $rt cycle
.\scripts\aseke-exec.ps1 --data-dir $rt status          # CLOSED (TAKE_PROFIT)
.\scripts\aseke-exec.ps1 scenario run all                # deterministic scenarios (own temp dirs)
```
Reference setup: LONG, leverage 5, entry 64500.10, SL 64000.10, TP 66000.10, margin 100.00
USDT, tolerance 100.00 → ceiling 64600.10. `sim_alpha` buys 7 contracts (0.007 BTC),
`sim_beta` 75 (0.0075 BTC). `publish-example` timestamps come from the current clock (expiry
10 min, max signal age 120 s).

## Strict signed-signal demonstration
```powershell
$rt = "runtime\strict-" + (Get-Date -Format yyyyMMddHHmmss)
$cfg = "config\sim-strict.toml"
.\scripts\aseke-exec.ps1 auth gen-key --out secrets\sim-demo-k1.key      # once; never printed
$env:ASEKE_EXEC_HMAC_SIM_DEMO_K1 = (Get-Content -Raw secrets\sim-demo-k1.key).Trim()
.\scripts\aseke-exec.ps1 --config $cfg --data-dir $rt init-runtime
.\scripts\aseke-exec.ps1 --config $cfg --data-dir $rt auth check          # secret_available: true
.\scripts\aseke-exec.ps1 --config $cfg --data-dir $rt publish-example long --signed --trade-id strict-1
.\scripts\aseke-exec.ps1 --config $cfg --data-dir $rt publish-example long --trade-id unsigned-1   # refused
.\scripts\aseke-exec.ps1 --config $cfg --data-dir $rt cycle
.\scripts\aseke-exec.ps1 --config $cfg --data-dir $rt status   # strict-1 protected; unsigned-1 quarantined
.\scripts\aseke-exec.ps1 --config $cfg --data-dir $rt health --json
```

## Operating commands
| Purpose | Command |
|---|---|
| Validate config / runtime fingerprint | `validate-config` · `runtime-info` |
| Fresh isolated runtime | `--data-dir runtime\X init-runtime` |
| Worker (foreground, Ctrl+C) | `run [--poll-seconds 2]` |
| One cycle / reconcile | `cycle` · `reconcile` |
| Status / health | `status [--json]` · `health [--json]` (exit 0 healthy) |
| Pause / resume NEW entries | `pause` · `resume` (queued to a running worker) |
| Account entries / unblock | `account disable|enable|unblock <id>` |
| Resolve incident | `incident resolve <id> --note "..."` |
| Publish signals | `publish-example long|short|multi-tp|timed [--signed]`, `close|cancel|amend --trade-id X` |
| Keys (strict mode) | `auth gen-key --out <file>` · `auth check` · `auth sign --in <event.json>` |
| Backup / restore | `backup --dest <new dir>` · `--data-dir <new root> restore --backup <dir>` |
| Soak | `--data-dir <new root> soak --duration-seconds N` or `--cycles N --accelerated` · `--funding-profile config|funded-test-100k|low-capital-test-50` (TEST-ONLY synthetic wallets) |
| Scenarios / simulator harness | `scenario list|run all` · `sim init|show|set-market|fault` |
| Tests / lint | `.\.venv-exec\Scripts\python.exe -m pytest --basetemp $env:TEMP\aseke-pt` · `.\.venv-exec\Scripts\python.exe -m pyflakes src tests scripts` |

Pause and account-disable stop **new entries only**. Only one worker can run per runtime root
(OS lock). Nothing installs a Windows service or scheduled task. Never delete a runtime root
or SQLite sidecar files as routine recovery; initialise a new root instead.

## Limitations (summary)
Synthetic venues only; no liquidation/funding model; separate (non-bracket) protective orders;
source-risk metadata informational only; amendments ABSOLUTE only; HMAC is shared-key
authentication only; the original 24 h soak proves duration and stability, not sustained entry
coverage (its 1,000 USDT synthetic wallets were exhausted). Full lists:
[Phase 2A](docs/phase2a-acceptance-report.md#limitations),
[Phase 2B](docs/phase2b-acceptance-report.md#remaining-limitations),
[Phase 2C](docs/phase2c-acceptance-report.md#remaining-limitations).
