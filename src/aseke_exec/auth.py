"""Authenticated signal envelope ``aseke.exec.envelope/1`` (HMAC-SHA-256, stdlib only).

Specification: docs/signal-envelope.md. Summary of the signing bytes::

    b"aseke.exec.envelope/1\\n" + canonical_json({
        "alg", "audience", "envelope_version", "environment", "issued_at", "key_id",
        "nonce", "payload", "producer"})

where canonical_json = json.dumps(obj, sort_keys=True, separators=(",", ":"),
ensure_ascii=False, allow_nan=False).encode("utf-8") over a document that contains only
objects, arrays, strings, integers, booleans and null (no floats: decimal amounts are
strings). ``signature`` = lowercase hex HMAC-SHA-256(key, signing_bytes).

Threat boundary: a shared-key HMAC proves possession of an authorized key and content
integrity. It is NOT encryption, NOT asymmetric proof of origin, and does NOT protect
against compromise of the Windows account/host that stores the key.

Order of checks (all before any trade/event state is created):
parse (strict: duplicate keys, floats, NaN rejected) -> structure -> key known -> key not
revoked -> secret available (else HOLD: file left in the inbox, fail closed) -> signature
(hmac.compare_digest) -> key scope (producer / environment / audience) -> inner payload
consistency + strategy scope -> issued_at skew/age (action specific).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets as _secrets
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .clock import Clock, iso, parse_utc
from .reasons import R, Rejection

ENVELOPE_VERSION = "aseke.exec.envelope/1"
ALG = "HMAC-SHA256"
SIGNING_PREFIX = b"aseke.exec.envelope/1\n"
SIGNED_FIELDS = ("alg", "audience", "envelope_version", "environment", "issued_at", "key_id",
                 "nonce", "payload", "producer")
ENVELOPE_FIELDS = frozenset(SIGNED_FIELDS + ("signature",))
MODE_STRICT = "strict"
MODE_COMPAT = "simulation-compat-unsigned"
MIN_KEY_BYTES = 32

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{2,99}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[0-9a-f]{16,64}$")


class AuthRejection(Rejection):
    """``hold=True`` means fail closed WITHOUT consuming the file (e.g. key not configured)."""

    def __init__(self, code: R, message: str, hold: bool = False, **details: Any):
        super().__init__(code, message, **details)
        self.hold = hold


# ------------------------------------------------------------------ canonical form
def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise AuthRejection(R.DUPLICATE_JSON_KEY, f"duplicate JSON key {k!r}")
        out[k] = v
    return out


def _no_float(s: str) -> Any:
    raise AuthRejection(R.INVALID_NUMBER, f"JSON number {s} is ambiguous; decimals must be strings")


def _no_const(s: str) -> Any:
    raise AuthRejection(R.INVALID_NUMBER, f"non-finite JSON constant {s} is not allowed")


def strict_loads(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_float=_no_float,
                          parse_constant=_no_const)
    except AuthRejection:
        raise
    except RecursionError:
        raise AuthRejection(R.INVALID_JSON, "JSON nesting too deep") from None
    except (json.JSONDecodeError, ValueError) as e:
        raise AuthRejection(R.INVALID_JSON, f"not valid JSON: {e}") from None


def _check_types(obj: Any, path: str = "$") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise AuthRejection(R.ENVELOPE_INVALID, f"{path}: non-string key")
            _check_types(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check_types(v, f"{path}[{i}]")
    elif isinstance(obj, (float, Decimal)):
        raise AuthRejection(R.INVALID_NUMBER, f"{path}: float values cannot be signed; use strings")
    elif obj is not None and not isinstance(obj, (str, int, bool)):
        raise AuthRejection(R.ENVELOPE_INVALID, f"{path}: unsupported type {type(obj).__name__}")


def canonical_bytes(obj: Any) -> bytes:
    try:
        _check_types(obj)
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except UnicodeEncodeError:  # e.g. a lone surrogate "\ud800": not valid UTF-8 text
        raise AuthRejection(R.ENVELOPE_INVALID, "string is not encodable as UTF-8") from None
    except RecursionError:
        raise AuthRejection(R.ENVELOPE_INVALID, "document nesting too deep") from None


def signing_bytes(envelope: dict[str, Any]) -> bytes:
    return SIGNING_PREFIX + canonical_bytes({k: envelope[k] for k in SIGNED_FIELDS})


def compute_signature(key: bytes, envelope: dict[str, Any]) -> str:
    return hmac.new(key, signing_bytes(envelope), hashlib.sha256).hexdigest()


def build_envelope(payload: dict[str, Any], *, key: bytes, key_id: str, audience: str,
                   issued_at: datetime, nonce: str | None = None) -> dict[str, Any]:
    """Wrap an unsigned v1 event. producer/environment are copied from the payload."""
    env = {
        "envelope_version": ENVELOPE_VERSION, "alg": ALG, "key_id": key_id,
        "producer": payload["producer"]["name"], "environment": payload["environment"],
        "audience": audience, "issued_at": iso(issued_at),
        "nonce": nonce or _secrets.token_hex(16), "payload": payload,
    }
    env["signature"] = compute_signature(key, env)
    return env


def generate_key_hex() -> str:
    return _secrets.token_hex(MIN_KEY_BYTES)


# ------------------------------------------------------------------ key material
@dataclass(frozen=True)
class KeySpec:
    key_id: str
    producer: str
    strategies: frozenset[str]
    environments: frozenset[str]
    audiences: frozenset[str]
    status: str  # active | revoked
    secret_env: str | None = None
    secret_file: Path | None = None

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class AuthConfig:
    mode: str
    audience: str
    keys: dict[str, KeySpec]
    max_future_skew_seconds: int = 5
    max_open_issued_age_seconds: int = 120
    max_exit_issued_age_seconds: int = 86400
    signer_key_id: str | None = None

    def fingerprint(self) -> dict[str, Any]:
        """Redacted description (no secrets, no secret paths' contents)."""
        return {"mode": self.mode, "audience": self.audience, "signer_key_id": self.signer_key_id,
                "keys": {k: {"producer": v.producer, "status": v.status,
                             "strategies": sorted(v.strategies),
                             "environments": sorted(v.environments),
                             "secret_source": "env" if v.secret_env else "file"}
                         for k, v in sorted(self.keys.items())}}


def load_secret(spec: KeySpec) -> bytes | None:
    """Read key material from the configured env var or excluded secret file. Never logged."""
    raw: str | None = None
    if spec.secret_env:
        raw = os.environ.get(spec.secret_env)
    elif spec.secret_file is not None:
        try:
            raw = spec.secret_file.read_text(encoding="utf-8")
        except OSError:
            raw = None
    if not raw:
        return None
    try:
        key = bytes.fromhex(raw.lstrip("\ufeff").strip())  # tolerate a UTF-8 BOM from editors
    except ValueError:
        return None
    return key if len(key) >= MIN_KEY_BYTES else None


# ------------------------------------------------------------------ verification
@dataclass(frozen=True)
class AuthResult:
    payload: dict[str, Any]
    mode: str  # HMAC | UNSIGNED_COMPAT
    key_id: str | None = None
    issued_at: datetime | None = None
    envelope_hash: str | None = None


class Authenticator:
    def __init__(self, cfg: AuthConfig, clock: Clock):
        self.cfg = cfg
        self.clock = clock

    def unwrap(self, text: str) -> AuthResult:
        doc = strict_loads(text)
        if not isinstance(doc, dict):
            raise AuthRejection(R.ENVELOPE_INVALID, "document must be a JSON object")
        is_env = "envelope_version" in doc or "signature" in doc
        if not is_env:
            if self.cfg.mode == MODE_COMPAT:
                return AuthResult(doc, "UNSIGNED_COMPAT")
            raise AuthRejection(R.AUTH_REQUIRED,
                                "strict mode: unsigned events are refused (no unsigned fallback)")
        return self._verify(doc)

    def _verify(self, env: dict[str, Any]) -> AuthResult:
        extra = set(env) - ENVELOPE_FIELDS
        missing = ENVELOPE_FIELDS - set(env)
        if extra or missing:
            raise AuthRejection(R.ENVELOPE_INVALID, f"envelope fields: missing {sorted(missing)}, "
                                                    f"unknown {sorted(extra)}")
        if env["envelope_version"] != ENVELOPE_VERSION or env["alg"] != ALG:
            raise AuthRejection(R.ENVELOPE_INVALID, "unsupported envelope_version/alg")
        for f in ("key_id", "producer"):
            if not isinstance(env[f], str) or not _ID.match(env[f]):
                raise AuthRejection(R.ENVELOPE_INVALID, f"invalid {f}")
        for f in ("environment", "audience", "issued_at"):
            if not isinstance(env[f], str) or not env[f]:
                raise AuthRejection(R.ENVELOPE_INVALID, f"invalid {f}")
        if not isinstance(env["nonce"], str) or not _NONCE.match(env["nonce"]):
            raise AuthRejection(R.ENVELOPE_INVALID, "nonce must be 16-64 lowercase hex chars")
        if not isinstance(env["signature"], str) or not _HEX64.match(env["signature"]):
            raise AuthRejection(R.ENVELOPE_INVALID, "signature must be 64 lowercase hex chars")
        payload = env["payload"]
        if not isinstance(payload, dict):
            raise AuthRejection(R.ENVELOPE_INVALID, "payload must be an object")
        spec = self.cfg.keys.get(env["key_id"])
        if spec is None:
            raise AuthRejection(R.AUTH_UNKNOWN_KEY, f"key_id {env['key_id']!r} is not configured")
        if not spec.active:
            raise AuthRejection(R.AUTH_KEY_REVOKED, f"key_id {spec.key_id!r} is revoked")
        key = load_secret(spec)
        if key is None:
            raise AuthRejection(R.AUTH_KEY_UNAVAILABLE,
                                f"secret for key_id {spec.key_id!r} is not available "
                                f"({'env ' + spec.secret_env if spec.secret_env else 'secret file'}); "
                                "failing closed, file left in inbox", hold=True)
        expected = compute_signature(key, env)
        if not hmac.compare_digest(expected, env["signature"]):
            raise AuthRejection(R.AUTH_BAD_SIGNATURE, "signature does not verify")
        # ---- authorization scope (signed outer fields)
        if env["producer"] != spec.producer:
            raise AuthRejection(R.AUTH_SCOPE_DENIED, "producer not authorized for this key")
        if env["environment"] not in spec.environments:
            raise AuthRejection(R.AUTH_SCOPE_DENIED, "environment not authorized for this key")
        if env["audience"] != self.cfg.audience or env["audience"] not in spec.audiences:
            raise AuthRejection(R.AUTH_SCOPE_DENIED, "envelope audience is not this executor")
        # ---- inner/outer consistency
        inner_prod = payload.get("producer", {}).get("name") if isinstance(payload.get("producer"), dict) else None
        if inner_prod != env["producer"] or payload.get("environment") != env["environment"]:
            raise AuthRejection(R.AUTH_ENVELOPE_MISMATCH,
                                "payload producer/environment differ from the signed envelope")
        if not isinstance(payload.get("strategy_id"), str) or payload["strategy_id"] not in spec.strategies:
            raise AuthRejection(R.AUTH_SCOPE_DENIED, "strategy not authorized for this key")
        # ---- action-specific timestamps
        try:
            issued = parse_utc(env["issued_at"])
        except ValueError as e:
            raise AuthRejection(R.AUTH_TIMESTAMP_INVALID, f"issued_at: {e}") from None
        now = self.clock.now()
        age = (now - issued).total_seconds()
        if age < -self.cfg.max_future_skew_seconds:
            raise AuthRejection(R.AUTH_TIMESTAMP_INVALID, f"issued_at is {-age:.1f}s in the future")
        is_open = payload.get("event_type") == "OPEN"
        max_age = self.cfg.max_open_issued_age_seconds if is_open else self.cfg.max_exit_issued_age_seconds
        if age > max_age:
            raise AuthRejection(R.AUTH_TIMESTAMP_INVALID,
                                f"issued_at age {age:.0f}s exceeds {max_age}s for "
                                f"{'OPEN' if is_open else 'non-OPEN'} events")
        try:
            created = parse_utc(payload.get("created_at"))
            if (created - issued).total_seconds() > self.cfg.max_future_skew_seconds:
                raise AuthRejection(R.AUTH_TIMESTAMP_INVALID, "issued_at precedes payload created_at")
        except ValueError:
            pass  # the contract parser reports the payload timestamp error
        return AuthResult(payload, "HMAC", spec.key_id, issued,
                          hashlib.sha256(signing_bytes(env)).hexdigest())
