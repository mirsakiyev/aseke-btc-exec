# CLAUDE.md

Read and follow **AGENTS.md** — it holds the binding project rules (Git rules, Phase 1
simulation-only boundary, confirmed trading behaviour, ECC usage). Non-negotiables:

1. Never commit/push/rewrite history/discard changes — the user does all Git writes.
   End every implementation response with a suggested commit subject + body for the actual
   changes (or state that no new commit is needed).
2. Simulation only. Live mode must fail before any order-capable activity. No real
   credentials, venues, services or scheduled tasks.
3. Do not touch the `aseke-btc-lab` repository or its database.
4. Use available ECC capabilities after checking they exist; don't change global ECC config,
   don't disable hooks, and describe actions accurately when a hook asks for facts.
5. Every test/demo/soak/restore uses its own `--data-dir runtime\<name>`; never delete `var\`
   or SQLite sidecars; only the worker writes the journal (WAL-reset-bug mitigation).
6. Keys never in config/logs/journal/CLI args; strict auth never falls back to unsigned.

Key paths: `src/aseke_exec/engine.py` (lifecycle), `planning.py` (pure math),
`contract.py` (signal v1 parser), `sim.py` (persistent simulator + adapter),
`journal.py` (SQLite ledger), `scenarios.py` (acceptance scenarios), `auth.py` (HMAC envelope),
`ops.py` (health/backup/restore/runtime-info), `soak.py`, `faults.py` (crash points), `docs/`
(runbook.md, signal-envelope.md, phase2a-acceptance-report.md).

`runtime_policy.py` (approved-runtime gate; pin in `config/runtime-lock.json`),
`scripts/setup.ps1` (provisioning), `scripts/storage_compat.py` (cross-runtime storage evidence),
`docs/phase2b-acceptance-report.md` (current verdict).

Commands: `.venv-exec\Scripts\python -m pytest --basetemp $env:TEMP\aseke-pt` ·
`.\scripts\aseke-exec.ps1 scenario run all` · `.\scripts\aseke-exec.ps1 runtime-info`.
Never run operational commands on the old `.venv` (affected SQLite; refused by the gate).
