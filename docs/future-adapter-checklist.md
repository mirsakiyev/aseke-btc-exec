# Future real-adapter checklist (NOT part of Phase 1)

No real adapter exists yet. Before any venue adapter (e.g. a future MEXC, KuCoin or Kraken
implementation) can even be built for testing, each item must be verified against the
venue's **current official** documentation and test environment, with evidence recorded.

## Eligibility
- [ ] Account type, jurisdiction and product eligibility confirmed for the operator.
- [ ] Product is a BTC **linear perpetual** with explicit quote/settlement currency; the
      normalized identity (`BTC/<quote>:<settle>`) maps to exactly one native symbol.
- [ ] USD vs USDT (vs USDC) settlement explicitly distinguished; no implicit mapping.

## API and permissions
- [ ] Current official API version and base URLs; deprecation notices reviewed.
- [ ] API key permissions limited to trading on the specific sub-account; **withdrawal
      disabled**; IP allow-listing configured; keys stored outside the repository.
- [ ] Rate limits and back-off behaviour documented; per-endpoint weights known.

## Instrument rules (refreshed through the adapter, never hard-coded in strategy logic)
- [ ] Contract multiplier / contract value and its currency.
- [ ] Tick size, quantity step, minimum quantity, minimum notional, max order size.
- [ ] Leverage range per instrument and tier (risk limits); behaviour when exceeded.
- [ ] Isolated margin availability; how mode/leverage are set; behaviour with open positions.

## Signing and time
- [ ] Request signing algorithm and canonical string; nonce/timestamp rules.
- [ ] Server time endpoint; allowed clock skew; `recvWindow`-style limits.

## Orders and protection semantics
- [ ] Marketable-limit IOC support and its exact fill/cancel reporting.
- [ ] Client order id: allowed charset/length, uniqueness window, idempotency behaviour.
- [ ] Query by client id after a timeout (including recently-cancelled/filled orders).
- [ ] Stop/TP trigger types (LAST / MARK / INDEX) and their exact trigger semantics.
- [ ] Reduce-only / close-position flags; guarantee that exits cannot increase or reverse.
- [ ] Attached (bracket) protection behaviour on partial fills; whether creation is atomic
      (assume not); amend support for trigger orders (qty and price).
- [ ] Position mode (one-way vs hedge) and how it is enforced.
- [ ] Fill reporting: fill ids, fees and fee currency, duplicates, delays; websocket vs REST.
- [ ] Liquidation, ADL, funding schedule and their reporting.

## Errors and outcomes
- [ ] Map every venue error to `AdapterRejected` (definitive) vs `AdapterTimeout`
      (unknown outcome) vs `AdapterUnavailable` (read failure). Never map an ambiguous
      error to "rejected".
- [ ] Capabilities (`adapter.Capabilities`) declared truthfully; engine checks them.

## Testing
- [ ] Official testnet/sandbox availability and its differences from production.
- [ ] Contract tests replaying recorded responses; no live keys in CI.
- [ ] The full deterministic scenario suite still passes against the simulator.
- [ ] A separate, explicitly approved phase defines any live-mode enablement, the trusted
      authenticated Lab producer, and operator sign-off. Phase 1 code refuses live mode.
