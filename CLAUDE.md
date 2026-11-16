# CLAUDE.md

Read and follow **AGENTS.md** — it holds the binding project rules (Git rules, Phase 1
simulation-only boundary, confirmed trading behaviour, ECC usage). Non-negotiables:

1. Never commit/push/rewrite history/discard changes — the user does all Git writes.
   End every implementation response with a suggested commit subject + body for the actual
   changes (or state that no new commit is needed).
2. Simulation only. Live mode must fail before any order-capable activity. No real
   credentials, venues, services or scheduled tasks.
3. Do not touch the `aseke-btc-lab` repository or its database.
4. Use available ECC capabilities after checking they exist; don't change global ECC config.

Key paths: `src/aseke_exec/engine.py` (lifecycle), `planning.py` (pure math),
`contract.py` (signal v1 parser), `sim.py` (persistent simulator + adapter),
`journal.py` (SQLite ledger), `scenarios.py` (acceptance scenarios), `docs/`.

Commands: `.venv\Scripts\python -m pytest` · `.\scripts\aseke-exec.ps1 scenario run all`
