"""Capability-aware exchange adapter interface.

Phase 1 ships only the deterministic simulator implementation (``sim.SimAdapter``).
Future real adapters (see docs/future-adapter-checklist.md) must implement this
interface and declare their capabilities honestly; the engine checks them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from .domain import Direction, InstrumentRules, MarginMode, PriceRef, Side


@dataclass(frozen=True)
class Capabilities:
    venue: str
    simulated: bool
    isolated_margin: bool
    cross_margin: bool
    trigger_references: frozenset[PriceRef]
    reduce_only: bool
    amend_protective: bool
    ioc_limit: bool
    client_order_ids: bool
    taker_fee_rate: Decimal
    # Native entry-attached brackets. Phase 1 simulators declare False, so the engine
    # always uses the verified separate protective-order flow.
    attached_protection: bool = False


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    bids: tuple[tuple[Decimal, Decimal], ...]  # best first (desc)
    asks: tuple[tuple[Decimal, Decimal], ...]  # best first (asc)
    ts: datetime

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0][0] if self.asks else None


@dataclass(frozen=True)
class ReferencePrices:
    """LAST, MARK and INDEX are distinct references. The executable price is the book."""

    last: Decimal
    mark: Decimal
    index: Decimal
    ts: datetime

    def get(self, ref: PriceRef) -> Decimal:
        return {PriceRef.LAST: self.last, PriceRef.MARK: self.mark, PriceRef.INDEX: self.index}[ref]


@dataclass(frozen=True)
class Balance:
    currency: str
    wallet: Decimal
    available: Decimal


@dataclass(frozen=True)
class Position:
    symbol: str
    side: Direction | None
    qty: Decimal  # contracts, >= 0
    avg_price: Decimal | None
    margin: Decimal
    leverage: int | None
    margin_mode: MarginMode | None = None


class OrderKind(StrEnum):
    ENTRY_LIMIT_IOC = "ENTRY_LIMIT_IOC"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    CLOSE_MARKET = "CLOSE_MARKET"


class OrderStatus(StrEnum):
    OPEN = "OPEN"  # working (trigger orders) - never for IOC entries
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"  # incl. IOC remainder cancelled after a partial fill
    EXPIRED = "EXPIRED"  # IOC accepted, nothing filled
    REJECTED = "REJECTED"


TERMINAL_ORDER = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED,
                            OrderStatus.REJECTED})


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: Side
    kind: OrderKind
    qty: Decimal
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    trigger_reference: PriceRef | None = None
    reduce_only: bool = False


@dataclass(frozen=True)
class FillReport:
    fill_id: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    ts: datetime


@dataclass(frozen=True)
class OrderReport:
    client_order_id: str
    venue_order_id: str
    symbol: str
    side: Side
    kind: OrderKind
    status: OrderStatus
    qty: Decimal
    filled_qty: Decimal
    avg_price: Decimal | None
    trigger_price: Decimal | None = None
    fills: tuple[FillReport, ...] = field(default_factory=tuple)
    reject_reason: str | None = None


class AdapterError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class AdapterRejected(AdapterError):
    """The venue definitively refused the request (no state change)."""


class AdapterTimeout(AdapterError):
    """Outcome UNKNOWN. The request may or may not have been accepted/filled."""


class AdapterUnavailable(AdapterError):
    """Could not reach the venue for a read/query."""


class ExchangeAdapter(ABC):
    venue: str
    account_id: str

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def instrument_rules(self, symbol: str) -> InstrumentRules: ...

    @abstractmethod
    def order_book(self, symbol: str) -> OrderBook: ...

    @abstractmethod
    def reference_prices(self, symbol: str) -> ReferencePrices: ...

    @abstractmethod
    def balance(self) -> Balance: ...

    @abstractmethod
    def position(self, symbol: str) -> Position: ...

    @abstractmethod
    def positions(self) -> list[Position]: ...

    @abstractmethod
    def set_margin(self, symbol: str, mode: MarginMode, leverage: int) -> None: ...

    @abstractmethod
    def place_order(self, req: OrderRequest) -> OrderReport: ...

    @abstractmethod
    def get_order(self, symbol: str, client_order_id: str) -> OrderReport | None: ...

    @abstractmethod
    def open_orders(self, symbol: str) -> list[OrderReport]: ...

    @abstractmethod
    def amend_order(self, symbol: str, client_order_id: str, *, qty: Decimal | None = None,
                    trigger_price: Decimal | None = None) -> OrderReport: ...

    @abstractmethod
    def cancel_order(self, symbol: str, client_order_id: str) -> OrderReport: ...
