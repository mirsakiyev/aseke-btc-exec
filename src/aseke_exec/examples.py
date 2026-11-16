"""Example signal builders. Timestamps always come from the supplied clock time,
so documented commands never publish an already-expired signal."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .clock import iso
from .contract import SCHEMA_VERSION

DUMMY_PRODUCER = "aseke-dummy-publisher"
DEFAULT_STRATEGY = "lab_btc_breakout"
INSTRUMENT = {"base": "BTC", "quote": "USDT", "settle": "USDT", "product_type": "LINEAR_PERPETUAL"}


def _common(now: datetime, trade_id: str, event_type: str, revision: int, *, event_id: str | None,
            producer: str, strategy_id: str, environment: str, instrument: dict[str, str] | None
            ) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "environment": environment,
        "producer": {"name": producer, "instance": "example"},
        "event_id": event_id or f"{trade_id}-r{revision}-{event_type.lower()}",
        "trade_id": trade_id,
        "strategy_id": strategy_id,
        "strategy_version": "1.0.0",
        "event_type": event_type,
        "revision": revision,
        "created_at": iso(now),
        "instrument": dict(instrument or INSTRUMENT),
    }


def build_open(now: datetime, trade_id: str, *, direction: str = "LONG", leverage: int = 5,
               reference_entry: str = "64500.10", stop: str | None = None,
               take_profits: list[tuple[str, str]] | None = None, level_policy: str = "ABSOLUTE",
               trigger_reference: str = "LAST", expires_in_s: int = 600,
               holding: dict[str, Any] | None = None, source_risk: dict[str, Any] | None = None,
               producer: str = DUMMY_PRODUCER, strategy_id: str = DEFAULT_STRATEGY,
               environment: str = "SIMULATION", event_id: str | None = None,
               source_quote: str = "USDT", instrument: dict[str, str] | None = None) -> dict[str, Any]:
    if level_policy == "ABSOLUTE":
        stop = stop or ("64000.10" if direction == "LONG" else "65000.10")
        tps = take_profits or ([("66000.10", "1")] if direction == "LONG" else [("63000.10", "1")])
        exits = {"level_policy": "ABSOLUTE", "trigger_reference": trigger_reference,
                 "stop": {"price": stop},
                 "take_profits": [{"price": p, "quantity_fraction": f} for p, f in tps]}
    else:
        tps = take_profits or [("0.02", "1")]
        exits = {"level_policy": "ENTRY_RELATIVE", "trigger_reference": trigger_reference,
                 "stop": {"distance_fraction": stop or "0.01"},
                 "take_profits": [{"distance_fraction": d, "quantity_fraction": f} for d, f in tps]}
    ev = _common(now, trade_id, "OPEN", 1, event_id=event_id, producer=producer,
                 strategy_id=strategy_id, environment=environment, instrument=instrument)
    ev["source"] = {"venue": "lab_source_feed", "instrument": "BTCUSDT",
                    "reference_price_type": "LAST", "quote_currency": source_quote,
                    "data_time": iso(now)}
    ev["open"] = {"direction": direction, "leverage": leverage, "entry_intent": "IMMEDIATE_IOC",
                  "reference_entry": reference_entry,
                  "expires_at": iso(now + timedelta(seconds=expires_in_s)), "exits": exits}
    if holding:
        ev["open"]["holding"] = holding
    if source_risk:
        ev["source_risk"] = source_risk
    return ev


def build_amend(now: datetime, trade_id: str, revision: int, *, stop: str | None = None,
                take_profits: list[tuple[str, str]] | None = None, trigger_reference: str = "LAST",
                holding: dict[str, Any] | None = None, event_id: str | None = None,
                producer: str = DUMMY_PRODUCER, strategy_id: str = DEFAULT_STRATEGY) -> dict[str, Any]:
    ev = _common(now, trade_id, "AMEND_EXITS", revision, event_id=event_id, producer=producer,
                 strategy_id=strategy_id, environment="SIMULATION", instrument=None)
    amend: dict[str, Any] = {}
    if stop is not None:
        amend["exits"] = {"level_policy": "ABSOLUTE", "trigger_reference": trigger_reference,
                          "stop": {"price": stop},
                          "take_profits": [{"price": p, "quantity_fraction": f}
                                           for p, f in (take_profits or [])]}
    if holding is not None:
        amend["holding"] = holding
    ev["amend"] = amend
    return ev


def build_close(now: datetime, trade_id: str, revision: int, *, reason: str = "lab exit signal",
                event_id: str | None = None, producer: str = DUMMY_PRODUCER,
                strategy_id: str = DEFAULT_STRATEGY) -> dict[str, Any]:
    ev = _common(now, trade_id, "CLOSE", revision, event_id=event_id, producer=producer,
                 strategy_id=strategy_id, environment="SIMULATION", instrument=None)
    ev["close"] = {"reason": reason}
    return ev


def build_cancel(now: datetime, trade_id: str, revision: int, *, event_id: str | None = None,
                 producer: str = DUMMY_PRODUCER, strategy_id: str = DEFAULT_STRATEGY) -> dict[str, Any]:
    ev = _common(now, trade_id, "CANCEL_ENTRY", revision, event_id=event_id, producer=producer,
                 strategy_id=strategy_id, environment="SIMULATION", instrument=None)
    ev["cancel_entry"] = {"reason": "lab cancelled entry"}
    return ev


EXAMPLE_KINDS = {
    "long": dict(direction="LONG"),
    "short": dict(direction="SHORT"),
    "multi-tp": dict(direction="LONG", take_profits=[("65000.10", "0.5"), ("66000.10", "0.5")]),
    "timed": dict(direction="LONG", holding={"max_holding_seconds": 900}),
}
