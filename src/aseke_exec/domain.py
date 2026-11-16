"""Core domain types: enums and immutable value objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .clock import iso, parse_utc
from .num import D, s


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def entry_side(self) -> Side:
        return Side.BUY if self is Direction.LONG else Side.SELL

    @property
    def exit_side(self) -> Side:
        return Side.SELL if self is Direction.LONG else Side.BUY

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1


class Environment(StrEnum):
    SIMULATION = "SIMULATION"
    LIVE = "LIVE"


class EventType(StrEnum):
    OPEN = "OPEN"
    AMEND_EXITS = "AMEND_EXITS"
    CLOSE = "CLOSE"
    CANCEL_ENTRY = "CANCEL_ENTRY"


class EntryIntent(StrEnum):
    IMMEDIATE_IOC = "IMMEDIATE_IOC"


class LevelPolicy(StrEnum):
    ABSOLUTE = "ABSOLUTE"
    ENTRY_RELATIVE = "ENTRY_RELATIVE"


class PriceRef(StrEnum):
    LAST = "LAST"
    MARK = "MARK"
    INDEX = "INDEX"


class ProductType(StrEnum):
    LINEAR_PERPETUAL = "LINEAR_PERPETUAL"
    INVERSE_PERPETUAL = "INVERSE_PERPETUAL"
    LINEAR_FUTURE = "LINEAR_FUTURE"
    INVERSE_FUTURE = "INVERSE_FUTURE"
    SPOT = "SPOT"


SUPPORTED_PRODUCTS = frozenset({ProductType.LINEAR_PERPETUAL})
SUPPORTED_BASES = frozenset({"BTC"})


class MarginMode(StrEnum):
    ISOLATED = "ISOLATED"
    CROSS = "CROSS"


class SourceRiskBasis(StrEnum):
    UNSPECIFIED = "UNSPECIFIED"
    ACCOUNT_EQUITY = "ACCOUNT_EQUITY"
    INITIAL_MARGIN = "INITIAL_MARGIN"
    PRICE_DISTANCE = "PRICE_DISTANCE"
    SOURCE_PAPER_EQUITY = "SOURCE_PAPER_EQUITY"


@dataclass(frozen=True)
class InstrumentId:
    """Normalized target instrument identity (venue-independent)."""

    base: str
    quote: str
    settle: str
    product_type: ProductType

    @property
    def key(self) -> str:
        return f"{self.base}/{self.quote}:{self.settle}"

    def to_dict(self) -> dict[str, str]:
        return {"base": self.base, "quote": self.quote, "settle": self.settle,
                "product_type": str(self.product_type)}


@dataclass(frozen=True)
class InstrumentRules:
    """Venue-native contract metadata, refreshed through the adapter boundary."""

    venue: str
    native_symbol: str
    base: str
    quote: str
    settle: str
    product_type: ProductType
    contract_multiplier: Decimal  # BTC per contract
    tick_size: Decimal
    qty_step: Decimal  # contracts
    min_qty: Decimal  # contracts
    max_leverage: int
    min_leverage: int = 1

    @property
    def instrument_key(self) -> str:
        return f"{self.base}/{self.quote}:{self.settle}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue, "native_symbol": self.native_symbol, "base": self.base,
            "quote": self.quote, "settle": self.settle, "product_type": str(self.product_type),
            "contract_multiplier": s(self.contract_multiplier), "tick_size": s(self.tick_size),
            "qty_step": s(self.qty_step), "min_qty": s(self.min_qty),
            "max_leverage": self.max_leverage, "min_leverage": self.min_leverage,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "InstrumentRules":
        return InstrumentRules(
            venue=d["venue"], native_symbol=d["native_symbol"], base=d["base"], quote=d["quote"],
            settle=d["settle"], product_type=ProductType(d["product_type"]),
            contract_multiplier=D(d["contract_multiplier"]), tick_size=D(d["tick_size"]),
            qty_step=D(d["qty_step"]), min_qty=D(d["min_qty"]),
            max_leverage=int(d["max_leverage"]), min_leverage=int(d.get("min_leverage", 1)),
        )


@dataclass(frozen=True)
class TakeProfitSpec:
    quantity_fraction: Decimal
    price: Decimal | None = None
    distance_fraction: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"quantity_fraction": s(self.quantity_fraction)}
        if self.price is not None:
            d["price"] = s(self.price)
        if self.distance_fraction is not None:
            d["distance_fraction"] = s(self.distance_fraction)
        return d


@dataclass(frozen=True)
class ExitSpec:
    level_policy: LevelPolicy
    trigger_reference: PriceRef
    take_profits: tuple[TakeProfitSpec, ...]
    stop_price: Decimal | None = None
    stop_distance: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        stop: dict[str, Any] = {}
        if self.stop_price is not None:
            stop["price"] = s(self.stop_price)
        if self.stop_distance is not None:
            stop["distance_fraction"] = s(self.stop_distance)
        return {"level_policy": str(self.level_policy),
                "trigger_reference": str(self.trigger_reference),
                "stop": stop, "take_profits": [t.to_dict() for t in self.take_profits]}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ExitSpec":
        stop = d["stop"]
        return ExitSpec(
            level_policy=LevelPolicy(d["level_policy"]),
            trigger_reference=PriceRef(d["trigger_reference"]),
            stop_price=D(stop["price"]) if "price" in stop else None,
            stop_distance=D(stop["distance_fraction"]) if "distance_fraction" in stop else None,
            take_profits=tuple(
                TakeProfitSpec(
                    quantity_fraction=D(t["quantity_fraction"]),
                    price=D(t["price"]) if "price" in t else None,
                    distance_fraction=D(t["distance_fraction"]) if "distance_fraction" in t else None,
                ) for t in d["take_profits"]),
        )


@dataclass(frozen=True)
class HoldingRule:
    close_at: datetime | None = None
    max_holding_seconds: int | None = None

    def deadline(self, first_fill_at: datetime | None) -> datetime | None:
        if self.close_at is not None:
            return self.close_at
        if self.max_holding_seconds is not None and first_fill_at is not None:
            return first_fill_at + timedelta(seconds=self.max_holding_seconds)
        return None

    def to_dict(self) -> dict[str, Any]:
        if self.close_at is not None:
            return {"close_at_utc": iso(self.close_at)}
        return {"max_holding_seconds": self.max_holding_seconds}

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> "HoldingRule | None":
        if not d:
            return None
        if "close_at_utc" in d:
            return HoldingRule(close_at=parse_utc(d["close_at_utc"]))
        return HoldingRule(max_holding_seconds=int(d["max_holding_seconds"]))


@dataclass(frozen=True)
class SourceInfo:
    venue: str
    instrument: str
    reference_price_type: PriceRef
    quote_currency: str
    data_time: datetime | None


@dataclass(frozen=True)
class SourceRisk:
    """Informational only. Never used to move stops or cap sizing in Phase 1."""

    risk_fraction: Decimal
    basis: SourceRiskBasis
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"risk_fraction": s(self.risk_fraction), "basis": str(self.basis), "note": self.note}


@dataclass(frozen=True)
class OpenPayload:
    direction: Direction
    leverage: int
    entry_intent: EntryIntent
    reference_entry: Decimal
    expires_at: datetime
    exits: ExitSpec
    holding: HoldingRule | None


@dataclass(frozen=True)
class AmendPayload:
    exits: ExitSpec | None
    holding: HoldingRule | None


@dataclass(frozen=True)
class Event:
    schema_version: str
    environment: Environment
    producer_name: str
    producer_instance: str | None
    event_id: str
    trade_id: str
    strategy_id: str
    strategy_version: str
    event_type: EventType
    revision: int
    created_at: datetime
    instrument: InstrumentId
    source: SourceInfo | None = None
    open: OpenPayload | None = None
    amend: AmendPayload | None = None
    reason: str | None = None
    source_risk: SourceRisk | None = None
