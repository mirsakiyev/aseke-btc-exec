"""Strict contract: documented valid/invalid examples, schema agreement, canonical hashing."""

import json
from pathlib import Path

import pytest

from aseke_exec.contract import content_hash, load_event_json, parse_event
from aseke_exec.domain import EventType, LevelPolicy
from aseke_exec.reasons import Rejection

ROOT = Path(__file__).resolve().parents[1]
VALID = sorted((ROOT / "examples/signals/valid").glob("*.json"))
INVALID_DIR = ROOT / "examples/signals/invalid"
EXPECTED = json.loads((INVALID_DIR / "EXPECTED.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.name)
def test_valid_examples_parse(path):
    ev = parse_event(load_event_json(path.read_text(encoding="utf-8")))
    assert ev.event_id and ev.trade_id


@pytest.mark.parametrize("name,code", sorted(EXPECTED.items()))
def test_invalid_examples_rejected_with_reason_code(name, code):
    with pytest.raises(Rejection) as ei:
        parse_event(load_event_json((INVALID_DIR / name).read_text(encoding="utf-8")))
    assert str(ei.value.code) == code, ei.value.message
    assert ei.value.message  # human-readable explanation always present


def test_reference_example_fields_preserved_exactly():
    ev = parse_event(load_event_json((ROOT / "examples/signals/valid/open_long_reference.json").read_text()))
    assert ev.event_type is EventType.OPEN and ev.open is not None
    assert str(ev.open.reference_entry) == "64500.10" and ev.open.leverage == 5
    assert ev.open.exits.level_policy is LevelPolicy.ABSOLUTE
    assert str(ev.open.exits.stop_price) == "64000.10"
    assert str(ev.open.exits.take_profits[0].price) == "66000.10"
    assert ev.source_risk is not None and str(ev.source_risk.risk_fraction) == "0.002"


def test_missing_level_policy_never_implies_entry_relative():
    raw = load_event_json((ROOT / "examples/signals/valid/open_long_reference.json").read_text())
    del raw["open"]["exits"]["level_policy"]
    with pytest.raises(Rejection) as ei:
        parse_event(raw)
    assert str(ei.value.code) == "MISSING_FIELD"


def test_json_infinity_constant_rejected():
    with pytest.raises(Rejection) as ei:
        load_event_json('{"x": Infinity}')
    assert str(ei.value.code) == "INVALID_NUMBER"


def test_canonical_hash_ignores_key_order_and_whitespace():
    a = {"b": 1, "a": {"y": "2", "x": "1"}}
    b = json.loads('{ "a": {"x": "1", "y": "2"},   "b": 1 }')
    assert content_hash(a) == content_hash(b)


def test_json_schema_agrees_with_examples():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "schemas/signal.v1.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    v = jsonschema.Draft202012Validator(schema)
    for p in VALID:
        errs = list(v.iter_errors(json.loads(p.read_text(encoding="utf-8"))))
        assert not errs, (p.name, [e.message for e in errs])
    # structural invalid examples must also fail the schema (semantic ones are executor-only)
    structural = ["float_price.json", "naive_timestamp.json", "non_utc_offset.json", "leverage_101.json",
                  "unknown_field.json", "credentials_in_signal.json", "unknown_schema_version.json",
                  "entry_relative_with_price.json", "open_missing_expiry.json",
                  "close_with_open_payload.json", "nan_price.json", "holding_both_forms.json"]
    for name in structural:
        assert not v.is_valid(json.loads((INVALID_DIR / name).read_text(encoding="utf-8"))), name
