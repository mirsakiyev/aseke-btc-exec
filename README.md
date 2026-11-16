# ASEKE BTC Exec — Phase 1 (simulation only)

ASEKE BTC **Lab** decides which trading opportunities to authorize. ASEKE BTC **Exec**
(this repository) receives those authorizations, validates them, sizes positions from its
own per-account configuration, executes through adapters, manages protection and exits,
and reconciles state.

> **Phase 1 is simulation-only.** No real exchange orders, no API credentials, no
> authenticated accounts, no fund transfers. `mode = "live"` and `LIVE` events are refused
> before any order-capable activity. The synthetic venues `sim_alpha` / `sim_beta` are not
> models of any real exchange. Nothing here validates real-exchange behaviour or implies
> profitability.

## What it does
- Strict versioned JSON signal contract (`aseke.exec.signal/1`) — see
  [docs/signal-contract.md](docs/signal-contract.md), [schema](schemas/signal.v1.schema.json),
  [examples](examples/signals/).
- Atomic file inbox → durable SQLite journal → per-account execution lifecycle:
  validation → sizing/reservation → IOC entry → verified exchange-held protection →
  TP/SL/time/Lab exits → reconciliation, incidents and alerts.
- A deterministic, **persistent** simulated exchange (its own SQLite file) with order-book
  IOC matching, partial fills, fees, isolated margin, LAST/MARK/INDEX triggers, reduce-only
  closes and fault injection (rejects, timeouts before/after acceptance, stale/duplicate
  observations, book shifts).
- Standalone worker; no Claude/LLM session, dashboard, broker or service required.

Architecture and lifecycle: [docs/architecture.md](docs/architecture.md).
Acceptance evidence: [docs/acceptance-report.md](docs/acceptance-report.md).
Future real adapters: [docs/future-adapter-checklist.md](docs/future-adapter-checklist.md).
Project rules for contributors/agents: [AGENTS.md](AGENTS.md).

## Windows setup (PowerShell, from the repository root)

```powershell
# one-time: create .venv with Python 3.12 and install (runtime is stdlib-only; dev: pytest, jsonschema)
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1

# equivalent manual steps
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

All commands below use the thin wrapper `.\scripts\aseke-exec.ps1`, which runs
`.\.venv\Scripts\python.exe -m aseke_exec` with `config\sim.toml`. If script execution is
restricted, prefix with `powershell -ExecutionPolicy Bypass -File`, or call
`.\.venv\Scripts\python.exe -m aseke_exec <command>` directly. Runtime state (journal,
simulator, inbox/archive/quarantine, logs, `status.json`, `alerts.jsonl`) lives in `var\`
(git-ignored). Keep it on a local disk.

## Reproducible demonstration (no network, no keys)

```powershell
.\scripts\aseke-exec.ps1 validate-config
.\scripts\aseke-exec.ps1 sim init --reset                       # fresh synthetic venues @ 64500
.\scripts\aseke-exec.ps1 publish-example long --trade-id demo-long-1
.\scripts\aseke-exec.ps1 cycle                                  # reconcile + ingest + execute + protect
.\scripts\aseke-exec.ps1 status                                 # both accounts OPEN_PROTECTED
.\scripts\aseke-exec.ps1 sim show                               # venue positions + working SL/TP
.\scripts\aseke-exec.ps1 sim set-market --price 66100           # TP triggers on the venues
.\scripts\aseke-exec.ps1 cycle
.\scripts\aseke-exec.ps1 status                                 # CLOSED (TAKE_PROFIT)
.\scripts\aseke-exec.ps1 scenario run all                       # 27 deterministic acceptance scenarios
```

`publish-example` stamps `created_at`/`expires_at` from the current clock, so it never
publishes an already-expired signal (expiry 10 minutes; max signal age 120 s — run `cycle`
within two minutes, or keep the worker running with `run`). Reusing a `--trade-id` is
correctly rejected (`DUPLICATE_OPEN_CONFLICT`); pick a new id. `sim init --reset` resets
only the simulated exchange — if you reset it while the journal still holds open trades,
the next cycle will (correctly) report the venue/journal mismatch; delete `var\` for a fully
fresh start. `.\scripts\demo.ps1` runs the sequence above with a timestamped trade id.

The example is the supplied reference setup: LONG, leverage 5, reference entry 64500.10,
SL 64000.10, TP 66000.10, margin 100.00 USDT per account, adverse tolerance 100.00 →
buy ceiling 64600.10 (SHORT floor would be 64400.10). `sim_alpha` (0.001 BTC/contract)
gets 7 contracts = 0.007 BTC; `sim_beta` (0.0001 BTC/contract, step 5) gets 75 contracts =
0.0075 BTC — comparable exposure from different native quantities.

Other examples: `publish-example short|multi-tp|timed`, and
`publish-example close|cancel|amend --trade-id <id> --revision <n>`.
Partial-fill and failure behaviour is demonstrated by `scenario run ioc_outcomes
protection_failure close_paths …` (see `scenario list`).

## Operating commands

| Purpose | Command |
|---|---|
| Validate configuration | `.\scripts\aseke-exec.ps1 validate-config` |
| Run worker (foreground, Ctrl+C) | `.\scripts\aseke-exec.ps1 run` |
| One maintenance/ingestion cycle | `.\scripts\aseke-exec.ps1 cycle` |
| Status / JSON status | `.\scripts\aseke-exec.ps1 status` / `status --json` |
| Pause / resume NEW entries | `.\scripts\aseke-exec.ps1 pause` / `resume` |
| Disable / enable an account for entries | `.\scripts\aseke-exec.ps1 account disable sim_beta_b1` |
| Clear an entry block (after incidents resolved) | `.\scripts\aseke-exec.ps1 account unblock sim_beta_b1` |
| Resolve an incident after review | `.\scripts\aseke-exec.ps1 incident resolve 3 --note "checked"` |
| Reconcile against venues | `.\scripts\aseke-exec.ps1 reconcile` |
| Publish example signals | `.\scripts\aseke-exec.ps1 publish-example long` |
| Deterministic scenarios | `.\scripts\aseke-exec.ps1 scenario list` / `scenario run all` |
| Simulator harness (works while worker is down) | `sim init / show / set-market / fault` |
| Tests | `.\.venv\Scripts\python.exe -m pytest` |

Pause and account-disable stop **new entries only**; protection, exits, time exits and
reconciliation continue. Only one worker can run (`var\worker.lock`); a second instance is
refused. Nothing installs a Windows service or scheduled task.

## Phase 1 limitations (summary)
Synthetic venues only; no liquidation or funding model; native attached brackets are not
used (separate verified protective orders always); source-risk enforcement is not
implemented (metadata is informational); amendments must be ABSOLUTE; single worker process.
Full list: [docs/acceptance-report.md](docs/acceptance-report.md#limitations).
