# ASEKE BTC Exec — Phase 2A (simulation only)

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
[Phase 1](docs/acceptance-report.md), [Phase 2A](docs/phase2a-acceptance-report.md).
Future real adapters: [docs/future-adapter-checklist.md](docs/future-adapter-checklist.md).
Rules for contributors/agents: [AGENTS.md](AGENTS.md).

> **Runtime note:** the embedded SQLite here (3.49.1) is affected by the upstream
> WAL-reset bug. A single-writer topology mitigates it, but unattended operation is not
> certified until a project-isolated runtime with SQLite ≥ 3.51.3 (or 3.50.7 / 3.44.6) is used.
> Check with `.\scripts\aseke-exec.ps1 runtime-info`. Details: [runbook](docs/runbook.md).

## Windows setup (PowerShell, repository root)
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
# or manually:
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```
Every command goes through `.\scripts\aseke-exec.ps1` (= `.\.venv\Scripts\python.exe -m
aseke_exec`). Use `--data-dir runtime\<name>` to give every demo/test/soak its **own runtime
root** (git-ignored). Without `--data-dir` the config's `data_dir` (`var\`) is used.

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
| Soak | `--data-dir <new root> soak --duration-seconds N` or `--cycles N --accelerated` |
| Scenarios / simulator harness | `scenario list|run all` · `sim init|show|set-market|fault` |
| Tests | `.\.venv\Scripts\python.exe -m pytest` |

Pause and account-disable stop **new entries only**. Only one worker can run per runtime root
(OS lock). Nothing installs a Windows service or scheduled task. Never delete a runtime root
or SQLite sidecar files as routine recovery; initialise a new root instead.

## Limitations (summary)
Synthetic venues only; no liquidation/funding model; separate (non-bracket) protective orders;
source-risk metadata informational only; amendments ABSOLUTE only; HMAC is shared-key
authentication only; affected embedded SQLite (see above). Full list:
[docs/phase2a-acceptance-report.md](docs/phase2a-acceptance-report.md#limitations).
