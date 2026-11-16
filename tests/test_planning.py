"""Pure execution math: bounds, rounding, sizing, allocation, entry-relative, risk."""

from decimal import Decimal as Dec

import pytest

from aseke_exec.domain import Direction, ExitSpec, LevelPolicy, PriceRef, TakeProfitSpec
from aseke_exec.planning import (
    allocate_take_profits, check_geometry, downside_at_stop, entry_limit_price, pnl_at_stop,
    price_within_bound, resolve_levels, size_position,
)
from aseke_exec.reasons import R, Rejection

E, TOL = Dec("64500.10"), Dec("100.00")


def test_reference_bounds_have_correct_signs():
    assert entry_limit_price(Direction.LONG, E, TOL, Dec("0.1")) == Dec("64600.10")
    assert entry_limit_price(Direction.SHORT, E, TOL, Dec("0.1")) == Dec("64400.10")


@pytest.mark.parametrize("tick,long_bound,short_bound", [
    (Dec("0.05"), Dec("64600.10"), Dec("64400.10")),
    (Dec("0.5"), Dec("64600.0"), Dec("64400.5")),   # rounding tightens, never weakens
    (Dec("1"), Dec("64600"), Dec("64401")),
])
def test_tick_rounding_preserves_adverse_limit(tick, long_bound, short_bound):
    lb = entry_limit_price(Direction.LONG, E, TOL, tick)
    sb = entry_limit_price(Direction.SHORT, E, TOL, tick)
    assert (lb, sb) == (long_bound, short_bound)
    assert lb <= E + TOL and sb >= E - TOL


def test_boundary_price_is_inclusive_and_one_tick_beyond_is_not():
    b = entry_limit_price(Direction.LONG, E, TOL, Dec("0.1"))
    assert price_within_bound(Direction.LONG, b, b)
    assert not price_within_bound(Direction.LONG, b + Dec("0.1"), b)
    assert price_within_bound(Direction.LONG, E - Dec("500"), b)  # favourable: no lower limit
    s = entry_limit_price(Direction.SHORT, E, TOL, Dec("0.1"))
    assert price_within_bound(Direction.SHORT, s, s)
    assert not price_within_bound(Direction.SHORT, s - Dec("0.1"), s)
    assert price_within_bound(Direction.SHORT, E + Dec("500"), s)


def test_nonpositive_bound_rejected():
    with pytest.raises(Rejection) as ei:
        entry_limit_price(Direction.SHORT, Dec("50"), Dec("100"), Dec("0.1"))
    assert ei.value.code is R.INVALID_PRICE_BOUND


def test_margin_100_leverage_5_targets_500_notional_and_multipliers():
    a = size_position(Dec("100.00"), 5, Dec("64600.1"), Dec("0.001"), Dec("1"), Dec("1"))
    b = size_position(Dec("100.00"), 5, Dec("64600.1"), Dec("0.0001"), Dec("5"), Dec("10"))
    assert a.target_notional == b.target_notional == Dec("500.00")
    assert (a.planned_contracts, b.planned_contracts) == (Dec(7), Dec(75))
    assert (a.planned_qty_btc, b.planned_qty_btc) == (Dec("0.007"), Dec("0.0075"))
    for sz in (a, b):  # rounding is down: never more margin than budgeted at the sizing price
        assert sz.planned_notional <= sz.target_notional
        assert sz.planned_margin <= Dec("100.00")


def test_below_minimum_is_rejected_not_rounded_up():
    with pytest.raises(Rejection) as ei:
        size_position(Dec("1"), 1, Dec("64600"), Dec("0.001"), Dec("1"), Dec("1"))
    assert ei.value.code is R.QTY_BELOW_MINIMUM


def test_tp_allocation_floor_with_last_leg_residual():
    assert allocate_take_profits(Dec(7), [Dec("0.5"), Dec("0.5")], Dec(1), Dec(1)) == [Dec(3), Dec(4)]
    assert allocate_take_profits(Dec(75), [Dec("0.5"), Dec("0.5")], Dec(5), Dec(10)) == [Dec(35), Dec(40)]
    assert allocate_take_profits(Dec(10), [Dec("0.3"), Dec("0.3"), Dec("0.4")], Dec(1), Dec(1)) == [Dec(3), Dec(3), Dec(4)]


def test_tp_allocation_infeasible_and_bad_fractions():
    with pytest.raises(Rejection) as ei:
        allocate_take_profits(Dec(1), [Dec("0.5"), Dec("0.5")], Dec(1), Dec(1))
    assert ei.value.code is R.TP_ALLOCATION_INFEASIBLE
    with pytest.raises(Rejection) as ei:
        allocate_take_profits(Dec(10), [Dec("0.5"), Dec("0.4")], Dec(1), Dec(1))
    assert ei.value.code is R.INVALID_TP_FRACTIONS


def _rel(stop, tp):
    return ExitSpec(LevelPolicy.ENTRY_RELATIVE, PriceRef.LAST,
                    (TakeProfitSpec(Dec("1"), distance_fraction=Dec(tp)),), stop_distance=Dec(stop))


def test_entry_relative_long_and_short_from_actual_anchor_rounded_toward_entry():
    stop, tps = resolve_levels(Direction.LONG, _rel("0.01", "0.02"), Dec("64500.1"), Dec("0.1"))
    assert stop == Dec("63855.1") and tps == [Dec("65790.1")]  # 63855.099 up, 65790.102 down
    stop, tps = resolve_levels(Direction.SHORT, _rel("0.01", "0.02"), Dec("64499.9"), Dec("0.1"))
    assert stop == Dec("65144.8") and tps == [Dec("63210.0")]  # 65144.899 down, 63209.902 up


def test_absolute_levels_are_returned_unchanged():
    spec = ExitSpec(LevelPolicy.ABSOLUTE, PriceRef.LAST, (TakeProfitSpec(Dec(1), price=Dec("66000.10")),),
                    stop_price=Dec("64000.10"))
    assert resolve_levels(Direction.LONG, spec, Dec("64321.7"), Dec("0.1")) == (Dec("64000.10"), [Dec("66000.10")])


def test_geometry_rejects_cheap_long_through_stop():
    with pytest.raises(Rejection) as ei:
        check_geometry(Direction.LONG, Dec("63950"), Dec("64000.10"), [Dec("66000.10")])
    assert ei.value.code is R.STOP_ALREADY_BREACHED
    check_geometry(Direction.LONG, Dec("64300"), Dec("64000.10"), [Dec("66000.10")])  # favourable ok
    with pytest.raises(Rejection):
        check_geometry(Direction.SHORT, Dec("62900"), Dec("65000.10"), [Dec("63000.10")])


def test_risk_at_stop_nonnegative_and_signed_pnl():
    q = Dec("0.007")
    assert downside_at_stop(Direction.LONG, q, Dec("64500.1"), Dec("64000.1")) == Dec("3.5000")
    assert downside_at_stop(Direction.SHORT, q, Dec("64499.9"), Dec("65000.1")) == q * Dec("500.2")
    # profit-protecting amended stop: zero risk, positive signed P&L
    assert downside_at_stop(Direction.LONG, q, Dec("64500.1"), Dec("64600.1")) == 0
    assert pnl_at_stop(Direction.LONG, q, Dec("64500.1"), Dec("64600.1")) == Dec("0.7000")
    assert pnl_at_stop(Direction.SHORT, q, Dec("64500"), Dec("65000")) == Dec("-3.500")
