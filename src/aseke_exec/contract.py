"""Strict parser/validator for the versioned signal/event contract v1.

See docs/signal-contract.md and schemas/signal.v1.schema.json. Every rejection
carries a machine-readable reason code and a human-readable message.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Any

from .clock import parse_utc
from .domain import (
    SUPPORTED_BASES, SUPPORTED_PRODUCTS, AmendPayload, Direction, EntryIntent, Environment,
    Event, EventType, ExitSpec, HoldingRule, InstrumentId, LevelPolicy, OpenPayload, PriceRef,
    ProductType, SourceInfo, SourceRisk, SourceRiskBasis, TakeProfitSpec,
)
from .num import ONE, ZERO, D, is_decimal_string
from .planning import check_geometry
from .reasons import R, Rejection

SCHEMA_VERSION = "aseke.exec.signal/1"
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})
MAX_TAKE_PROFITS = 10
MAX_HOLDING_SECONDS = 30 * 24 * 3600
MAX_LEVERAGE = 100

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{2,99}$")
_CCY_RE = re.compile(r"^[A-Z0-9]{2,12}$")
PAYLOAD_KEYS = {EventType.OPEN: "open", EventType.AMEND_EXITS: "amend",
                EventType.CLOSE: "close", EventType.CANCEL_ENTRY: "cancel_entry"}


# ------------------------------------------------------------------ helpers
def _reject_constant(name: str) -> Any:
    raise Rejection(R.INVALID_NUMBER, f"non-finite JSON constant {name} is not allowed")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise Rejection(R.DUPLICATE_JSON_KEY, f"duplicate JSON key {k!r} (ambiguous input)")
        out[k] = v
    return out


def load_event_json(text: str) -> dict[str, Any]:
    """Strict JSON decode: duplicate keys and NaN/Infinity are rejected; JSON numbers with a
    fraction/exponent decode to Decimal (never float) and are then refused by the contract."""
    try:
        obj = json.loads(text, parse_float=Decimal, parse_constant=_reject_constant,
                         object_pairs_hook=_no_duplicate_keys)
    except Rejection:
        raise
    except (json.JSONDecodeError, ValueError) as e:
        raise Rejection(R.INVALID_JSON, f"not valid JSON: {e}") from None
    if not isinstance(obj, dict):
        raise Rejection(R.INVALID_TYPE, "event must be a JSON object")
    return obj


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return format(o, "f")
    raise TypeError(type(o).__name__)


def canonical_json(raw: dict[str, Any]) -> str:
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=_json_default)


def content_hash(raw: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()


class _Obj:
    def __init__(self, data: Any, path: str):
        if not isinstance(data, dict):
            raise Rejection(R.INVALID_TYPE, f"{path or 'event'} must be an object", path=path)
        self.d = data
        self.path = path
        self.seen: set[str] = set()

    def p(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def req(self, key: str) -> Any:
        self.seen.add(key)
        if key not in self.d or self.d[key] is None:
            raise Rejection(R.MISSING_FIELD, f"required field '{self.p(key)}' is missing",
                            path=self.p(key))
        return self.d[key]

    def opt(self, key: str) -> Any:
        self.seen.add(key)
        return self.d.get(key)

    def has(self, key: str) -> bool:
        return key in self.d

    def done(self) -> None:
        extra = sorted(set(self.d) - self.seen)
        if extra:
            raise Rejection(R.UNKNOWN_FIELD,
                            f"unknown field(s) at '{self.path or '<root>'}': {', '.join(extra)}",
                            path=self.path, fields=extra)


def _str(v: Any, path: str, max_len: int = 200, pattern: re.Pattern[str] | None = None,
         code: R = R.INVALID_TYPE) -> str:
    if not isinstance(v, str) or not v or len(v) > max_len:
        raise Rejection(code, f"'{path}' must be a non-empty string (max {max_len})", path=path)
    if pattern is not None and not pattern.match(v):
        raise Rejection(code, f"'{path}' has an invalid format", path=path, value=v)
    return v


def _id(v: Any, path: str) -> str:
    return _str(v, path, 100, _ID_RE, R.INVALID_IDENTIFIER)


def _enum(v: Any, cls: type, path: str) -> Any:
    if not isinstance(v, str):
        raise Rejection(R.INVALID_ENUM, f"'{path}' must be a string enum", path=path)
    try:
        return cls(v)
    except ValueError:
        allowed = [m.value for m in cls]  # type: ignore[attr-defined]
        raise Rejection(R.INVALID_ENUM, f"'{path}' = {v!r} is not one of {allowed}",
                        path=path, value=v) from None


def _dec(v: Any, path: str, *, positive: bool = True) -> Decimal:
    if not is_decimal_string(v):
        raise Rejection(R.INVALID_NUMBER,
                        f"'{path}' must be a finite decimal string such as \"64500.10\"",
                        path=path)
    d = D(v)
    if positive and d <= 0:
        raise Rejection(R.INVALID_NUMBER, f"'{path}' must be > 0", path=path, value=v)
    return d


def _int(v: Any, path: str, lo: int, hi: int, code: R = R.INVALID_NUMBER) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise Rejection(R.INVALID_TYPE, f"'{path}' must be an integer", path=path)
    if not lo <= v <= hi:
        raise Rejection(code, f"'{path}' must be within [{lo}, {hi}]", path=path, value=v)
    return v


def _ts(v: Any, path: str):
    try:
        return parse_utc(v)
    except (ValueError, TypeError) as e:
        raise Rejection(R.INVALID_TIMESTAMP, f"'{path}': {e}", path=path) from None


def _fraction(v: Any, path: str, *, inclusive_one: bool) -> Decimal:
    d = _dec(v, path)
    if d > ONE or (d == ONE and not inclusive_one):
        raise Rejection(R.INVALID_NUMBER, f"'{path}' must be < {'or = ' if inclusive_one else ''}1",
                        path=path, value=v)
    return d


# ----------------------------------------------------------------- sections
def _instrument(v: Any) -> InstrumentId:
    o = _Obj(v, "instrument")
    base = _str(o.req("base"), "instrument.base", 12, _CCY_RE)
    quote = _str(o.req("quote"), "instrument.quote", 12, _CCY_RE)
    settle = _str(o.req("settle"), "instrument.settle", 12, _CCY_RE)
    product = _enum(o.req("product_type"), ProductType, "instrument.product_type")
    o.done()
    if product not in SUPPORTED_PRODUCTS:
        raise Rejection(R.UNSUPPORTED_PRODUCT,
                        f"product type {product} is not supported (only LINEAR_PERPETUAL)")
    if base not in SUPPORTED_BASES:
        raise Rejection(R.UNSUPPORTED_INSTRUMENT, f"base asset {base} is not supported (BTC only)")
    if quote != settle:
        raise Rejection(R.CURRENCY_MAPPING_UNSUPPORTED,
                        f"linear perpetual must settle in its quote currency ({quote} != {settle})")
    return InstrumentId(base, quote, settle, product)


def _source(v: Any) -> SourceInfo:
    o = _Obj(v, "source")
    src = SourceInfo(
        venue=_str(o.req("venue"), "source.venue", 64),
        instrument=_str(o.req("instrument"), "source.instrument", 64),
        reference_price_type=_enum(o.req("reference_price_type"), PriceRef,
                                   "source.reference_price_type"),
        quote_currency=_str(o.req("quote_currency"), "source.quote_currency", 12, _CCY_RE),
        data_time=_ts(o.req("data_time"), "source.data_time"),
    )
    o.done()
    return src


def _exits(v: Any, path: str, *, allow_relative: bool) -> ExitSpec:
    o = _Obj(v, path)
    policy = _enum(o.req("level_policy"), LevelPolicy, f"{path}.level_policy")
    trig = _enum(o.req("trigger_reference"), PriceRef, f"{path}.trigger_reference")
    if policy is LevelPolicy.ENTRY_RELATIVE and not allow_relative:
        raise Rejection(R.AMEND_POLICY_UNSUPPORTED,
                        "amendments must use ABSOLUTE levels in contract v1")
    stop_o = _Obj(o.req("stop"), f"{path}.stop")
    stop_price = stop_dist = None
    if policy is LevelPolicy.ABSOLUTE:
        if stop_o.has("distance_fraction"):
            raise Rejection(R.INVALID_LEVEL_SPEC, "ABSOLUTE stop must not carry distance_fraction")
        stop_price = _dec(stop_o.req("price"), f"{path}.stop.price")
    else:
        if stop_o.has("price"):
            raise Rejection(R.INVALID_LEVEL_SPEC, "ENTRY_RELATIVE stop must not carry price")
        stop_dist = _fraction(stop_o.req("distance_fraction"), f"{path}.stop.distance_fraction",
                              inclusive_one=False)
    stop_o.done()
    tps_raw = o.req("take_profits")
    if not isinstance(tps_raw, list) or not 1 <= len(tps_raw) <= MAX_TAKE_PROFITS:
        raise Rejection(R.INVALID_TYPE,
                        f"'{path}.take_profits' must be a list of 1..{MAX_TAKE_PROFITS} targets")
    tps: list[TakeProfitSpec] = []
    for i, t in enumerate(tps_raw):
        tp = f"{path}.take_profits[{i}]"
        to = _Obj(t, tp)
        frac = _fraction(to.req("quantity_fraction"), f"{tp}.quantity_fraction", inclusive_one=True)
        if policy is LevelPolicy.ABSOLUTE:
            if to.has("distance_fraction"):
                raise Rejection(R.INVALID_LEVEL_SPEC, f"{tp}: ABSOLUTE target must not carry distance_fraction")
            tps.append(TakeProfitSpec(frac, price=_dec(to.req("price"), f"{tp}.price")))
        else:
            if to.has("price"):
                raise Rejection(R.INVALID_LEVEL_SPEC, f"{tp}: ENTRY_RELATIVE target must not carry price")
            tps.append(TakeProfitSpec(frac, distance_fraction=_fraction(
                to.req("distance_fraction"), f"{tp}.distance_fraction", inclusive_one=False)))
        to.done()
    o.done()
    if sum((t.quantity_fraction for t in tps), ZERO) != ONE:
        raise Rejection(R.INVALID_TP_FRACTIONS,
                        "take-profit quantity_fraction values must sum to exactly 1")
    levels = [t.price if t.price is not None else t.distance_fraction for t in tps]
    if len(set(levels)) != len(levels):
        raise Rejection(R.INVALID_GEOMETRY, "duplicate take-profit levels")
    return ExitSpec(policy, trig, tuple(tps), stop_price, stop_dist)


def _holding(v: Any, path: str, created_at) -> HoldingRule:
    o = _Obj(v, path)
    has_abs = o.has("close_at_utc")
    has_rel = o.has("max_holding_seconds")
    if has_abs and has_rel:
        raise Rejection(R.HOLDING_RULE_CONFLICT,
                        "holding rule must use close_at_utc OR max_holding_seconds, not both")
    if not has_abs and not has_rel:
        raise Rejection(R.HOLDING_RULE_CONFLICT,
                        "holding rule present but empty; omit it for no time exit")
    if has_abs:
        close_at = _ts(o.req("close_at_utc"), f"{path}.close_at_utc")
        o.done()
        if close_at <= created_at:
            raise Rejection(R.HOLDING_RULE_CONFLICT, "close_at_utc must be after created_at")
        return HoldingRule(close_at=close_at)
    secs = _int(o.req("max_holding_seconds"), f"{path}.max_holding_seconds", 1, MAX_HOLDING_SECONDS)
    o.done()
    return HoldingRule(max_holding_seconds=secs)


def check_exit_order(direction: Direction, spec: ExitSpec, code: R) -> None:
    """Stop and targets on opposite sides (ABSOLUTE only; anchor-free check)."""
    if spec.level_policy is not LevelPolicy.ABSOLUTE:
        return
    assert spec.stop_price is not None
    for t in spec.take_profits:
        assert t.price is not None
        ok = t.price > spec.stop_price if direction is Direction.LONG else t.price < spec.stop_price
        if not ok:
            raise Rejection(code, f"{direction} take-profit {t.price} is on the wrong side of stop "
                                  f"{spec.stop_price}")


def _open(v: Any, created_at) -> OpenPayload:
    o = _Obj(v, "open")
    direction = _enum(o.req("direction"), Direction, "open.direction")
    leverage = _int(o.req("leverage"), "open.leverage", 1, MAX_LEVERAGE, R.INVALID_LEVERAGE)
    intent = _enum(o.req("entry_intent"), EntryIntent, "open.entry_intent")
    ref = _dec(o.req("reference_entry"), "open.reference_entry")
    expires = _ts(o.req("expires_at"), "open.expires_at")
    exits = _exits(o.req("exits"), "open.exits", allow_relative=True)
    holding = _holding(o.opt("holding"), "open.holding", created_at) if o.has("holding") else None
    o.done()
    if expires <= created_at:
        raise Rejection(R.INVALID_EXPIRY, "open.expires_at must be after created_at")
    if exits.level_policy is LevelPolicy.ABSOLUTE:
        assert exits.stop_price is not None
        check_geometry(direction, ref, exits.stop_price, [t.price for t in exits.take_profits],  # type: ignore[misc]
                       R.INVALID_GEOMETRY, R.INVALID_GEOMETRY, "reference entry")
    return OpenPayload(direction, leverage, intent, ref, expires, exits, holding)


# -------------------------------------------------------------------- entry
def parse_event(raw: dict[str, Any]) -> Event:
    """Validate a decoded JSON object and return a typed Event or raise Rejection."""
    o = _Obj(raw, "")
    sv = o.req("schema_version")
    if sv not in SUPPORTED_SCHEMA_VERSIONS:
        raise Rejection(R.UNKNOWN_SCHEMA_VERSION, f"unsupported schema_version {sv!r}",
                        supported=sorted(SUPPORTED_SCHEMA_VERSIONS))
    env = _enum(o.req("environment"), Environment, "environment")
    po = _Obj(o.req("producer"), "producer")
    producer_name = _id(po.req("name"), "producer.name")
    producer_instance = (_str(po.opt("instance"), "producer.instance", 100)
                         if po.has("instance") else None)
    po.done()
    event_id = _id(o.req("event_id"), "event_id")
    trade_id = _id(o.req("trade_id"), "trade_id")
    strategy_id = _id(o.req("strategy_id"), "strategy_id")
    strategy_version = _str(o.req("strategy_version"), "strategy_version", 64)
    et = _enum(o.req("event_type"), EventType, "event_type")
    revision = _int(o.req("revision"), "revision", 1, 2**31 - 1, R.INVALID_REVISION)
    created_at = _ts(o.req("created_at"), "created_at")
    instrument = _instrument(o.req("instrument"))

    for etype, key in PAYLOAD_KEYS.items():
        if etype is not et and key in raw:
            raise Rejection(R.PAYLOAD_MISMATCH, f"'{key}' payload is not allowed on {et} events")
    if et is not EventType.OPEN and "source_risk" in raw:
        raise Rejection(R.PAYLOAD_MISMATCH, "source_risk is only allowed on OPEN events")

    source = _source(o.req("source")) if et is EventType.OPEN else (
        _source(o.opt("source")) if o.has("source") else None)

    ev_open = ev_amend = None
    reason: str | None = None
    source_risk: SourceRisk | None = None
    if et is EventType.OPEN:
        if revision != 1:
            raise Rejection(R.INVALID_REVISION, "OPEN must have revision 1")
        ev_open = _open(o.req("open"), created_at)
        if o.has("source_risk"):
            ro = _Obj(o.opt("source_risk"), "source_risk")
            source_risk = SourceRisk(
                risk_fraction=_fraction(ro.req("risk_fraction"), "source_risk.risk_fraction",
                                        inclusive_one=False),
                basis=_enum(ro.req("basis"), SourceRiskBasis, "source_risk.basis"),
                note=_str(ro.opt("note"), "source_risk.note", 500) if ro.has("note") else None)
            ro.done()
    else:
        if revision < 2:
            raise Rejection(R.INVALID_REVISION, f"{et} must have revision >= 2 (OPEN is 1)")
        key = PAYLOAD_KEYS[et]
        po2 = _Obj(o.req(key), key)
        if et is EventType.AMEND_EXITS:
            exits = (_exits(po2.opt("exits"), "amend.exits", allow_relative=False)
                     if po2.has("exits") else None)
            holding = (_holding(po2.opt("holding"), "amend.holding", created_at)
                       if po2.has("holding") else None)
            if exits is None and holding is None:
                raise Rejection(R.AMEND_EMPTY, "AMEND_EXITS must carry exits and/or holding")
            ev_amend = AmendPayload(exits, holding)
        else:
            reason = _str(po2.opt("reason"), f"{key}.reason", 200) if po2.has("reason") else None
        po2.done()
    o.done()
    return Event(sv, env, producer_name, producer_instance, event_id, trade_id, strategy_id,
                 strategy_version, et, revision, created_at, instrument, source, ev_open,
                 ev_amend, reason, source_risk)
