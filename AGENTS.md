# AGENTS.md — ASEKE BTC Exec working rules

These rules apply to every human or AI agent (Claude Code, ECC agents, others) working in
this repository. CLAUDE.md points here. Keep both in sync.

## Repository boundaries
- This is the **aseke-btc-exec** repository (https://github.com/mirsakiyev/aseke-btc-exec).
  Verify the checkout before editing. Preserve unrelated changes.
- **Never alter the separate `aseke-btc-lab` repository** and never read or modify Lab's
  internal database. Exec only consumes the published signal contract.

## Git rules (strict)
- The **user handles all commits and pushes**. Do not commit, push, rewrite history, merge,
  rebase, reset, or discard user changes. Read-only Git inspection is allowed.
- At the end of **every** implementation response, provide a suggested commit subject and
  body describing only the changes actually made. If a turn makes no changes, say
  explicitly that no new commit is needed.

## ECC / tooling
- Use the installed ECC plugin's relevant planning, architecture, implementation, testing
  and review capabilities after checking what is actually available. Do not invent
  commands, and do not reinstall, upgrade or change global ECC configuration.

## Phase 1 safety boundary (do not weaken without an explicit, agreed phase change)
- **Simulation is the only executable mode.** `mode = "live"` and `environment: LIVE`
  events must fail clearly before any order-capable activity.
- No real exchange orders, real API credentials, authenticated accounts, fund transfers,
  live exchange clients, dashboards, Telegram delivery, message brokers, Windows services or
  scheduled tasks in this phase. Never claim production readiness or profitability.
- Synthetic names only (`sim_alpha`, `sim_beta`, accounts prefixed `sim_`). Never label a
  simulator as a MEXC/KuCoin/Kraken adapter.
- Dummy signals are simulation-only. A producer-name string is not authentication.

## Confirmed trading behaviour (tested policies — see docs/)
- Capital = configured **initial margin per trade**, per account. M=100, L=5 -> target
  notional ~500 before execution details. Margin is a budget, not a promise.
- **Isolated margin only**; never fall back to cross.
- Requested leverage used **exactly** (1-100 and venue limits); unsupported -> reject, never reduce.
- OPEN = prompt marketable-limit **IOC**; no resting entries, no breakout reinterpretation.
- Entry tolerance applies to **adverse** movement only; favourable prices allowed if geometry stays valid.
- Absolute SL/TP are authoritative and never shifted. `ENTRY_RELATIVE` only when the signal
  explicitly requests it **and** account policy enables it (default: ABSOLUTE only).
- One strategy owner per account; one active trade per account/instrument; no pyramiding,
  reversal or aggregation. Fan-out to multiple accounts is independent; no automatic unwind.
- Lab authorizes new exposure; Exec manages/closes existing exposure (SL, TP, time exit,
  Lab close/update, protection-failure policy). No breakeven/trailing/invented logic.
- Partial IOC fills are protected and never topped up or chased.
- Config changes affect future entries only; existing trades keep their policy snapshot
  and original routing.
- Source risk percentages (e.g. informal "0.2%") are **informational only**; they never move
  the stop, cap sizing, or define an equity limit. Lab's 10,000 paper equity never sizes Exec.
- Decimal arithmetic only for money, prices, quantities, fees, fractions and leverage.

## Phase 2A operational rules (added)
- **Runtime roots:** every test/demo/soak/restore uses its own `--data-dir runtime\<name>`.
  Never recommend deleting the user's `var\` or any `*.db-wal`/`*.db-shm` sidecar as recovery;
  prefer `init-runtime` on a new root. `sim init --reset` is refused when the journal has history.
- **Single journal writer:** only the worker process writes the journal; operator commands queue
  control requests while a worker runs; status/health read with `mode=ro`. Keep it that way
  even on the patched runtime (defence in depth; never replace global DLLs or the global Python).
- **Approved runtime (Phase 2B):** only the project-private interpreter pinned in
  `config/runtime-lock.json` (`.venv-exec`, python.org CPython 3.15.2 amd64, loaded SQLite 3.53.4)
  may run operational commands; `runtime_policy.py` refuses others (exit 4) before touching a
  runtime root. Run everything via `scripts\aseke-exec.ps1` or `.venv-exec\Scripts\python.exe`.
  Never add an environment-variable bypass, never point tooling at the old `.venv` (kept only for
  diagnosis), never mark `evidence.extended_soak` complete without a reviewed operator soak.
  Changing the pinned build means updating the lock file, the dependency lock and re-running
  `scripts\storage_compat.py` plus the full regression (docs/phase2b-acceptance-report.md).
- **Authentication:** `[auth] mode` must be explicit (`strict` or `simulation-compat-unsigned`).
  Strict mode never falls back to unsigned. Keys only via env var / excluded secret file; never in
  config, journal, logs, reports, examples (except the public TEST-ONLY fixture key) or CLI args.
  Auth/authorization happens before any event, trade, tombstone, routing or reservation.
- **Protection:** stop coverage and TP coverage are separate; a TP failure with a verified stop is
  DEGRADED (bounded backoff, escalation incident, per-account entry gate), never a forced close.
  Healthy positions must produce no venue writes per cycle. One missing observation is never
  proof of absence (bounded not-found checks).
- **Crash points:** `ASEKE_EXEC_CRASHPOINT` (src/aseke_exec/faults.py) is a test hook; tests may
  kill only subprocesses they started. Never terminate user workers or touch aseke-btc-lab.
- Raw test counts are not a readiness gate; the readiness verdict lives in
  docs/phase2b-acceptance-report.md (Phase 2A evidence: docs/phase2a-acceptance-report.md).

## Engineering conventions
- Code stays compatible with Python 3.12+ (`requires-python`); the approved execution runtime is
  CPython 3.15.2. Stdlib-only runtime; dev deps (hash-locked in `requirements/`): pytest,
  jsonschema, pyflakes.
- Run tests: `.venv-exec\Scripts\python -m pytest --basetemp $env:TEMP\aseke-pt` (deterministic,
  offline). Keep `--basetemp` short: the setup test copies the repo under it and Windows
  PowerShell 5.1 has a ~248-character directory limit.
- Business invariants are asserted in `src/aseke_exec/scenarios.py` (shared by tests and
  `aseke-exec scenario run`). Add a scenario for every new lifecycle behaviour.
- Contract changes require a new `schema_version`, updated `schemas/`, `examples/`, and
  `docs/signal-contract.md`.
