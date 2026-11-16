"""Pure, deterministic execution math (no I/O).

Price eligibility, tick rounding, sizing, take-profit allocation, entry-relative
level resolution, geometry checks, and level-based risk at the stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence

from .domain import Direction, ExitSpec, LevelPolicy
from .num import ONE, ZERO, ceil_to_step, floor_to_step, is_multiple
from .reasons import R, Rejection


# ---------------------------------------------------------------- price bounds
def entry_limit_price(direction: Direction, reference_entry: Decimal, tolerance: Decimal,
                      tick: Decimal) -> Decimal:
    """Adverse-only bound. LONG ceiling = E + D rounded DOWN to tick;
    SHORT floor = E - D rounded UP to tick. Rounding never weakens the bound."""
    if reference_entry <= 0:
        raise Rejection(R.INVALID_PRICE_BOUND, "reference entry must be positive")
    if tolerance < 0:
        raise Rejection(R.INVALID_PRICE_BOUND, "adverse tolerance must be >= 0")
    if direction is Direction.LONG:
        bound = floor_to_step(reference_entry + tolerance, tick)
    else:
        bound = ceil_to_step(reference_entry - tolerance, tick)
    if bound <= 0:
        raise Rejection(R.INVALID_PRICE_BOUND,
                        f"computed {direction} price bound {bound} is not positive",
                        bound=str(bound))
    return bound


def price_within_bound(direction: Direction, price: Decimal, bound: Decimal) -> bool:
    return price <= bound if direction is Direction.LONG else price >= bound


def executable_depth(direction: Direction, levels: Sequence[tuple[Decimal, Decimal]],
                     bound: Decimal, qty: Decimal) -> tuple[Decimal, Decimal | None]:
    """Walk opposite-side levels (asks for LONG, bids for SHORT) inside the bound.
    Returns (fillable qty up to ``qty``, VWAP of that qty)."""
    remaining = qty
    filled = ZERO
    cost = ZERO
    for price, avail in levels:
        if remaining <= 0 or not price_within_bound(direction, price, bound):
            break
        take = min(avail, remaining)
        filled += take
        cost += take * price
        remaining -= take
    return filled, (cost / filled if filled > 0 else None)


# ---------------------------------------------------------------------- sizing
@dataclass(frozen=True)
class Sizing:
    requested_margin: Decimal
    leverage: int
    sizing_price: Decimal
    target_notional: Decimal  # M * L
    target_qty_btc: Decimal  # M * L / P
    raw_contracts: Decimal
    planned_contracts: Decimal
    planned_qty_btc: Decimal
    planned_notional: Decimal  # at sizing price
    planned_margin: Decimal  # planned_notional / L


def size_position(margin: Decimal, leverage: int, sizing_price: Decimal, multiplier: Decimal,
                  qty_step: Decimal, min_qty: Decimal) -> Sizing:
    """Target notional = M * L; contracts = floor_to_step(M * L / P / multiplier).
    Rounding is always DOWN, so planned margin never exceeds the configured budget at P."""
    if margin <= 0:
        raise Rejection(R.QTY_BELOW_MINIMUM, "configured margin must be positive")
    if sizing_price <= 0:
        raise Rejection(R.INVALID_PRICE_BOUND, "sizing price must be positive")
    lev = Decimal(leverage)
    target_notional = margin * lev
    target_btc = target_notional / sizing_price
    raw = target_btc / multiplier
    planned = floor_to_step(raw, qty_step)
    if planned < min_qty or planned <= 0:
        raise Rejection(R.QTY_BELOW_MINIMUM,
                        f"planned {planned} contracts below venue minimum {min_qty}",
                        planned=str(planned), min_qty=str(min_qty), raw=str(raw))
    planned_btc = planned * multiplier
    planned_notional = planned_btc * sizing_price
    return Sizing(margin, leverage, sizing_price, target_notional, target_btc, raw, planned,
                  planned_btc, planned_notional, planned_notional / lev)


# --------------------------------------------------------------- TP allocation
def allocate_take_profits(total_qty: Decimal, fractions: Sequence[Decimal], qty_step: Decimal,
                          min_qty: Decimal, code: R = R.TP_ALLOCATION_INFEASIBLE) -> list[Decimal]:
    """Policy: every leg except the last gets floor_to_step(total * fraction); the last
    leg absorbs the residual. Every leg must be >= min_qty. No leg is dropped and no
    equal split is invented. Fractions must sum to exactly 1."""
    if sum(fractions, ZERO) != ONE:
        raise Rejection(R.INVALID_TP_FRACTIONS, "take-profit fractions must sum to 1")
    if not is_multiple(total_qty, qty_step):
        raise Rejection(code, f"total qty {total_qty} not a multiple of step {qty_step}")
    out: list[Decimal] = []
    for f in fractions[:-1]:
        out.append(floor_to_step(total_qty * f, qty_step))
    out.append(total_qty - sum(out, ZERO))
    for i, q in enumerate(out):
        if q <= 0 or q < min_qty:
            raise Rejection(code, f"take-profit leg {i} quantity {q} is below venue minimum "
                                  f"{min_qty} for total {total_qty}",
                            leg=i, qty=str(q), total=str(total_qty), min_qty=str(min_qty))
    return out


# ------------------------------------------------------------------- levels
def resolve_levels(direction: Direction, spec: ExitSpec, anchor: Decimal | None,
                   tick: Decimal) -> tuple[Decimal, list[Decimal]]:
    """Return (stop, [tp prices]). ABSOLUTE levels are returned unchanged.
    ENTRY_RELATIVE levels are computed from ``anchor`` (actual VWAP entry) and rounded
    TOWARD the entry so neither the risk distance nor the target distance is widened."""
    if spec.level_policy is LevelPolicy.ABSOLUTE:
        assert spec.stop_price is not None
        return spec.stop_price, [t.price for t in spec.take_profits]  # type: ignore[misc]
    if anchor is None or anchor <= 0:
        raise Rejection(R.INVALID_LEVEL_SPEC, "entry-relative levels need an actual fill anchor")
    assert spec.stop_distance is not None
    if direction is Direction.LONG:
        stop = ceil_to_step(anchor * (ONE - spec.stop_distance), tick)
        tps = [floor_to_step(anchor * (ONE + t.distance_fraction), tick)  # type: ignore[operator]
               for t in spec.take_profits]
    else:
        stop = floor_to_step(anchor * (ONE + spec.stop_distance), tick)
        tps = [ceil_to_step(anchor * (ONE - t.distance_fraction), tick)  # type: ignore[operator]
               for t in spec.take_profits]
    return stop, tps


def check_geometry(direction: Direction, anchor: Decimal, stop: Decimal,
                   tps: Iterable[Decimal], stop_code: R = R.STOP_ALREADY_BREACHED,
                   tp_code: R = R.TP_ALREADY_REACHED, what: str = "expected fill") -> None:
    """LONG: stop < anchor < every TP.  SHORT: stop > anchor > every TP."""
    if direction is Direction.LONG:
        if not stop < anchor:
            raise Rejection(stop_code, f"LONG stop {stop} is not below the {what} {anchor}",
                            stop=str(stop), anchor=str(anchor))
        for tp in tps:
            if not tp > anchor:
                raise Rejection(tp_code, f"LONG take-profit {tp} is not above the {what} {anchor}",
                                tp=str(tp), anchor=str(anchor))
    else:
        if not stop > anchor:
            raise Rejection(stop_code, f"SHORT stop {stop} is not above the {what} {anchor}",
                            stop=str(stop), anchor=str(anchor))
        for tp in tps:
            if not tp < anchor:
                raise Rejection(tp_code, f"SHORT take-profit {tp} is not below the {what} {anchor}",
                                tp=str(tp), anchor=str(anchor))


def levels_are_tick_aligned(tick: Decimal, *prices: Decimal) -> bool:
    return all(is_multiple(p, tick) for p in prices)


# ---------------------------------------------------------------------- risk
def downside_at_stop(direction: Direction, qty_btc: Decimal, avg_fill: Decimal,
                     stop: Decimal) -> Decimal:
    """Non-negative estimated gross downside at the stop (level-based, before fees).
    A profit-protecting stop yields 0, never negative risk."""
    if direction is Direction.LONG:
        return max(ZERO, qty_btc * (avg_fill - stop))
    return max(ZERO, qty_btc * (stop - avg_fill))


def pnl_at_stop(direction: Direction, qty_btc: Decimal, avg_fill: Decimal, stop: Decimal) -> Decimal:
    """Signed estimated gross P&L if the stop executes exactly at its level."""
    return Decimal(direction.sign) * qty_btc * (stop - avg_fill)


def vwap(fills: Iterable[tuple[Decimal, Decimal]]) -> tuple[Decimal, Decimal | None]:
    qty = ZERO
    cost = ZERO
    for q, p in fills:
        qty += q
        cost += q * p
    return qty, (cost / qty if qty > 0 else None)
