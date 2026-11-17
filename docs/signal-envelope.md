# Authenticated signal envelope — `aseke.exec.envelope/1`

Optional strict authentication around the unchanged v1 event payload
([signal-contract.md](signal-contract.md)). Implementation: `src/aseke_exec/auth.py`
(standard-library `hmac` + `hashlib.sha256`, verification with `hmac.compare_digest`).
Deterministic fixture: [`examples/signals/envelopes/`](../examples/signals/envelopes/)
(its key is **TEST-ONLY and public** — see `FIXTURE.json`).

## Threat boundary (read this first)
A shared-key HMAC proves that the sender **possesses an authorized key** and that the
signed content was **not altered**. It is **not** encryption (envelopes are plain JSON),
**not** asymmetric proof of origin (anyone holding the key can sign), and gives **no**
protection if the Windows account/host that stores the key or the runtime root is
compromised. Anyone who can write the runtime root can also edit the journal directly.
The future Lab publisher will need its own compatible key and authorization scope; **no Lab
integration exists today**.

## Envelope
```json
{
  "envelope_version": "aseke.exec.envelope/1",
  "alg": "HMAC-SHA256",
  "key_id": "sim-demo-k1",
  "producer": "aseke-dummy-publisher",
  "environment": "SIMULATION",
  "audience": "aseke-exec:sim-local",
  "issued_at": "2026-01-05T12:00:01.000Z",
  "nonce": "0123456789abcdef0123456789abcdef",
  "payload": { "...": "unchanged aseke.exec.signal/1 event" },
  "signature": "<64 lowercase hex chars>"
}
```
Every field except `signature` is signed. Unknown or missing fields are rejected.

## Exact signing bytes
```
signing_bytes = b"aseke.exec.envelope/1\n" + canonical_json(
    {alg, audience, envelope_version, environment, issued_at, key_id, nonce, payload, producer})
signature     = lowercase hex( HMAC-SHA-256(key, signing_bytes) )
```
`canonical_json(x)` is exactly Python's
`json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")`:
keys sorted by Unicode code point at every level, no insignificant whitespace, UTF-8,
non-ASCII characters emitted literally, JSON escapes only where JSON requires them.
The document may contain only objects, arrays, strings, integers, booleans and null.
**Decimal amounts are strings** (`"64500.10"`); any JSON number with a fraction or exponent,
`NaN`/`Infinity`, duplicate object keys, lone surrogates, invalid UTF-8 and excessive nesting
are rejected before verification. Because the verifier re-canonicalizes the *parsed*
document, whitespace, key order and `\u` escapes in the transmitted file do not change the
verdict. A conforming non-Python producer must reproduce these bytes exactly; the fixture's
`signing_bytes_sha256` is the conformance check.

## Verification order (all before ANY event/trade/tombstone state)
1. strict parse (duplicate keys, floats, NaN, invalid UTF-8, nesting) → `INVALID`
2. structure / version / alg / nonce (16–64 lowercase hex) / signature format → `ENVELOPE_INVALID`
3. `key_id` configured → `AUTH_UNKNOWN_KEY`
4. key not revoked → `AUTH_KEY_REVOKED`
5. secret available → otherwise **HOLD**: the file stays in the inbox (fail closed), one
   durable `ingest_log` HELD record, existing positions keep being managed
6. signature (`hmac.compare_digest`) → `AUTH_BAD_SIGNATURE`
7. key scope: `producer`, `environment`, `audience` (this executor) → `AUTH_SCOPE_DENIED`
8. inner payload consistency: `payload.producer.name == producer`,
   `payload.environment == environment` → `AUTH_ENVELOPE_MISMATCH`; strategy in key scope
   → `AUTH_SCOPE_DENIED`
9. timestamps (action specific): future skew ≤ `max_future_skew_seconds` (5 s);
   OPEN `issued_at` age ≤ `max_open_issued_age_seconds` (default 120 s);
   AMEND/CLOSE/CANCEL age ≤ `max_exit_issued_age_seconds` (default 24 h) so a delayed,
   authorized close is still applied; `issued_at` may not precede payload `created_at`
   by more than the skew → `AUTH_TIMESTAMP_INVALID`.

Then the payload goes through the unchanged v1 contract parser and all Phase 1 rules
(entry expiry, signal age, ordering, tombstones). A signed file never bypasses entry expiry,
and an expired OPEN never causes an authorized close of existing exposure to be ignored.

Rejected files are quarantined with a reason file and a bounded `ingest_log` row
(first 2000 characters; never key material). They create **no** trade, event or tombstone,
so an invalid signed CLOSE cannot poison a later legitimate OPEN and a forged event cannot
consume a legitimate `event_id`. A file that cannot be processed at all is quarantined on
its own and never blocks the files behind it.

## Deduplication and replay
Event identity is the **payload**: the journal stores the payload's canonical hash.
A correctly re-signed duplicate (new nonce / `issued_at`) is the same event (`DUPLICATE`);
the same `event_id` with a different payload is `EVENT_ID_CONFLICT`. Revision ordering and
close-before-open tombstones are unchanged. Nonces are not tracked: replay protection is the
persistent `event_id` dedup plus the age windows. A captured non-OPEN envelope stays
verifiable for up to 24 h but has no second effect while its event remains in the journal
(the project never prunes events). **Do not restore an old journal backup and then re-feed
captured envelopes** — that is the residual replay window.

## Keys, scope, rotation, revocation (`[auth]`, see `config/sim-strict.toml`)
```toml
[auth]
mode = "strict"                       # or the explicit profile "simulation-compat-unsigned"
audience = "aseke-exec:sim-local"
[auth.signer]                         # local simulation publisher only
key_id = "sim-demo-k1"
[[auth.keys]]
key_id = "sim-demo-k1"
producer = "aseke-dummy-publisher"
strategies = ["lab_btc_breakout"]     # a key authorizes ONLY these strategies
environments = ["SIMULATION"]          # LIVE cannot be authorized in Phase 2A
audiences = ["aseke-exec:sim-local"]
status = "active"                      # "revoked" refuses it
secret_env = "ASEKE_EXEC_HMAC_SIM_DEMO_K1"   # or secret_file = "path/outside/the/repo"
```
* Key material is **never** in the config, the journal, logs, alerts, reports or on the
  command line. Config validation refuses inline `secret`/`key` fields.
* Generate: `aseke-exec auth gen-key --out secrets\sim-demo-k1.key` (256-bit random, created
  exclusively, owner-only on POSIX, never overwritten, contents never printed; warns if the
  path is inside the repository but outside `secrets\`). `secrets/` and `*.key` are
  git-ignored. On Windows restrict the file ACL to your account (not automated). Key files
  written with a UTF-8 BOM are accepted.
* Load: `$env:ASEKE_EXEC_HMAC_SIM_DEMO_K1 = (Get-Content -Raw secrets\sim-demo-k1.key).Trim()`
  (do not echo it). `aseke-exec auth check` reports availability only.
* **Rotation:** add the new key as a second active entry, move publishers to it, then set the
  old key to `revoked`. Key edits (status, scope, new keys) hot-reload.
* **Revocation policy for already-journaled events:** if an event's key is revoked, removed,
  or its producer/strategy/environment scope no longer covers the event *at processing time*,
  pending **OPEN/AMEND_EXITS are rejected** (`AUTH_KEY_REVOKED`). **CLOSE/CANCEL_ENTRY are
  still applied to trades that already have an OPEN** (they only reduce exposure), but they
  **never create a tombstone** for a trade without an OPEN.
* **Profile changes require a restart:** a hot reload that changes `auth.mode` or `audience`
  is refused with a CRITICAL `AUTH_PROFILE_CHANGE_REFUSED` alert. Strict mode never falls
  back to unsigned acceptance.
* **Compatibility profile** `simulation-compat-unsigned` (used by `config/sim.toml`, the
  scenarios and the soak) accepts unsigned fixtures and must be named explicitly; a config
  without `[auth] mode` is invalid. Unsigned events journaled under it are not applied as
  OPEN/AMEND if the worker is later restarted in strict mode.

## Signing locally (simulation)
```powershell
.\scripts\aseke-exec.ps1 --config config\sim-strict.toml --data-dir runtime\strict-demo publish-example long --signed
.\scripts\aseke-exec.ps1 --config config\sim-strict.toml --data-dir runtime\strict-demo auth sign --in my-event.json
```
