"""Decimal-only numeric helpers.

All prices, quantities, money, fractions, fees and leverage arithmetic use
``decimal.Decimal``. Binary floats are rejected at every boundary.
"""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, getcontext

getcontext().prec = 40

ZERO = Decimal(0)
ONE = Decimal(1)
FEE_QUANTUM = Decimal("0.00000001")

_DEC_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")


def is_decimal_string(value: object) -> bool:
    return isinstance(value, str) and bool(_DEC_RE.match(value))


def D(value: object) -> Decimal:
    """Convert a str/int/Decimal to a finite Decimal. Floats and bools are refused."""
    if isinstance(value, bool):
        raise TypeError("boolean is not a decimal value")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal")
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        st = value.strip()
        if not _DEC_RE.match(st):
            raise ValueError(f"invalid decimal string: {value!r}")
        return Decimal(st)
    if isinstance(value, float):
        raise TypeError("binary floats are not accepted; use a decimal string")
    raise TypeError(f"cannot convert {type(value).__name__} to Decimal")


def Dn(value: object) -> Decimal | None:
    return None if value is None else D(value)


def floor_to_step(x: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (x / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to_step(x: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (x / step).to_integral_value(rounding=ROUND_CEILING) * step


def is_multiple(x: Decimal, step: Decimal) -> bool:
    q = x / step
    return q == q.to_integral_value()


def quantize_fee(x: Decimal) -> Decimal:
    return x.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)


def s(x: Decimal | None) -> str | None:
    """Exact, exponent-free storage string."""
    if x is None:
        return None
    return format(x, "f")


def show(x: Decimal | None, places: int | None = None) -> str:
    """Human display string."""
    if x is None:
        return "-"
    if places is not None:
        x = x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        return format(x, "f")
    return format(x.normalize(), "f")
