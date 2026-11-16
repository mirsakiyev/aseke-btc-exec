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

## Engineering conventions
- Python 3.12+, stdlib-only runtime; dev deps: pytest, jsonschema.
- Run tests: `.venv\Scripts\python -m pytest` (deterministic, no network, no sleeps).
- Business invariants are asserted in `src/aseke_exec/scenarios.py` (shared by tests and
  `aseke-exec scenario run`). Add a scenario for every new lifecycle behaviour.
- Contract changes require a new `schema_version`, updated `schemas/`, `examples/`, and
  `docs/signal-contract.md`.
