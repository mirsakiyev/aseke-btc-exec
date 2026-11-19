"""Deterministic synthetic workload for the SIMULATION soak harness (Phase 2C).

The soak and the offline replay share this module, so a replay at the same seed reproduces the
exact price path and OPEN signal sequence of a soak run (the market walk never depends on engine
state). Nothing here touches a database, the journal, a clock or the network.

Random walk (unchanged from Phase 2A/2B, including the order of RNG draws, so historical runs can
be replayed): each cycle draws one step from ``STEPS``; with probability ``JUMP_PROBABILITY`` the
step is replaced by one of ``JUMPS``; the price is floored to ``WALK_TICK`` and to ``PRICE_FLOOR``.
On every ``trade_every_cycles``-th cycle one more draw picks LONG/SHORT. No other draws exist.

Exit-level policy (Phase 2C, ``LEVEL_POLICY``): the base absolute offsets stop 500, TP1 800,
TP2 1500 (quote units) are kept unchanged whenever every level BELOW the reference stays at or
above ``MIN_LEVEL_FRACTION`` of the reference. Otherwise all three offsets of that one signal are
scaled by the same factor (bound / largest below-reference offset), then floored to the
instruments' common tick. Direction geometry (LONG: stop < ref < TP1 < TP2; SHORT:
TP2 < TP1 < ref < stop), the 0.5/0.5 TP fractions and the RNG stream are unchanged; nothing is
clamped to zero or to the entry, no direction is flipped and no signal is skipped. If a level
still cannot be built validly the generator raises :class:`SoakWorkloadError` - the soak counts
it and never publishes it.

The pre-2C arithmetic is kept as :func:`legacy_exit_levels` ONLY for replaying historical runs.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from math import lcm
from typing import Any, Iterable

from .contract import load_event_json, parse_event
from .examples import build_open
from .num import D, floor_to_step, is_multiple, s
from .reasons import Rejection

LEVEL_POLICY = ("aseke.exec.soak-levels/2: base offsets stop 500 / TP1 800 / TP2 1500; when a "
                "below-reference level would sit under 50% of the reference, all three offsets "
                "are scaled by one factor and floored to the common tick")
STEPS = (-40, -25, -10, 0, 10, 25, 40)
JUMPS = (-900, 900, 1600, -1600)
JUMP_PROBABILITY = 0.06
PRICE_FLOOR = Decimal("1000")
WALK_TICK = Decimal("0.1")
BASE_OFFSETS = (Decimal(500), Decimal(800), Decimal(1500))  # stop, TP1, TP2 distances
MIN_LEVEL_FRACTION = Decimal("0.5")  # every level stays >= 50% of the reference
TP_FRACTIONS = ("0.5", "0.5")


class SoakWorkloadError(Exception):
    """The harness failed to build a valid CLEAN-workload signal. Never published."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ExitLevels:
    stop: Decimal
    take_profits: tuple[Decimal, Decimal]
    scale: Decimal  # 1 = base offsets unchanged


class MarketWalk:
    """Seeded random walk. ``advance`` and ``draw_long`` consume the RNG exactly as the Phase
    2A/2B soak loop did (same calls, same order)."""

    def __init__(self, seed: int, initial_price: Decimal):
        self.rng = random.Random(seed)
        self.price = D(initial_price)

    def advance(self) -> Decimal:
        step = Decimal(self.rng.choice(STEPS))
        if self.rng.random() < JUMP_PROBABILITY:
            step = Decimal(self.rng.choice(JUMPS))
        self.price = max(PRICE_FLOOR, floor_to_step(self.price + step, WALK_TICK))
        return self.price

    def draw_long(self) -> bool:
        return self.rng.random() < 0.5


def common_tick(ticks: Iterable[Decimal]) -> Decimal:
    """Coarsest grid every tick divides (least common multiple of decimal tick sizes)."""
    ts = [D(t) for t in ticks]
    if not ts or any(t <= 0 for t in ts):
        raise SoakWorkloadError("TICK_UNAVAILABLE", "no positive instrument tick size configured")
    places = max(max(0, -int(t.as_tuple().exponent)) for t in ts)
    scale = Decimal(10) ** places
    m = 1
    for t in ts:
        m = lcm(m, int(t * scale))
    return Decimal(m) / scale


def legacy_exit_levels(ref: Decimal, long_: bool) -> ExitLevels:
    """Phase 2A/2B arithmetic (REPLAY ONLY): yields nonpositive SHORT TP2 when ref <= 1500."""
    o0, o1, o2 = BASE_OFFSETS
    if long_:
        return ExitLevels(ref - o0, (ref + o1, ref + o2), Decimal(1))
    return ExitLevels(ref + o0, (ref - o1, ref - o2), Decimal(1))


def exit_levels(ref: Decimal, long_: bool, tick: Decimal) -> ExitLevels:
    ref, tick = D(ref), D(tick)
    if ref <= 0 or not is_multiple(ref, tick):
        raise SoakWorkloadError("REFERENCE_INVALID", f"reference {ref} is not positive on tick {tick}")
    below = BASE_OFFSETS[0] if long_ else BASE_OFFSETS[2]  # largest offset placed below the ref
    bound = ref * (1 - MIN_LEVEL_FRACTION)
    scale = min(Decimal(1), bound / below)
    offs = [floor_to_step(o * scale, tick) for o in BASE_OFFSETS] if scale < 1 else list(BASE_OFFSETS)
    if not (0 < offs[0] < offs[1] < offs[2]):
        raise SoakWorkloadError("LEVELS_DEGENERATE",
                                f"offsets {[s(o) for o in offs]} not distinct/positive at ref {ref}")
    if long_:
        lv = ExitLevels(ref - offs[0], (ref + offs[1], ref + offs[2]), scale)
    else:
        lv = ExitLevels(ref + offs[0], (ref - offs[1], ref - offs[2]), scale)
    if min(lv.stop, *lv.take_profits) <= 0:  # unreachable by construction; checked anyway
        raise SoakWorkloadError("LEVEL_NONPOSITIVE", f"nonpositive level at ref {ref}")
    return lv


def build_soak_open(now: datetime, trade_id: str, ref: Decimal, long_: bool, levels: ExitLevels, *,
                    max_holding_seconds: int, producer: str | None = None) -> dict[str, Any]:
    kw = {"producer": producer} if producer else {}
    return build_open(now, trade_id, direction="LONG" if long_ else "SHORT", reference_entry=s(ref),
                      stop=s(levels.stop),
                      take_profits=[(s(levels.take_profits[0]), TP_FRACTIONS[0]),
                                    (s(levels.take_profits[1]), TP_FRACTIONS[1])],
                      holding={"max_holding_seconds": max_holding_seconds}, **kw)


def validate_generated(ev: dict[str, Any], ticks: Iterable[Decimal]) -> None:
    """Run the PRODUCTION strict decoder + contract parser on the payload exactly as it is
    serialised, then require every price to sit on every configured instrument tick. Raises
    :class:`SoakWorkloadError`; the contract itself is not loosened in any way."""
    try:
        parsed = parse_event(load_event_json(json.dumps(ev)))
    except Rejection as r:
        raise SoakWorkloadError(getattr(r.code, "value", str(r.code)), str(r)) from None
    op = parsed.open
    if op is None or op.exits.stop_price is None:
        raise SoakWorkloadError("NOT_ABSOLUTE_OPEN", "soak workload must be an ABSOLUTE OPEN")
    prices = [op.reference_entry, op.exits.stop_price, *(t.price for t in op.exits.take_profits)]
    for p in prices:
        for t in ticks:
            if p is None or not is_multiple(p, D(t)):
                raise SoakWorkloadError("TICK_MISALIGNED", f"price {p} is not a multiple of tick {t}")


def replay(*, seed: int, cycles: int, trade_every_cycles: int, initial_price: Decimal,
           ticks: Iterable[Decimal], legacy: bool = False, now: datetime | None = None,
           max_holding_seconds: int = 30) -> dict[str, Any]:
    """Offline replay of the soak workload (no engine, no clock, no I/O). With ``legacy=True``
    the pre-2C levels are generated, to reproduce historical invalid candidates."""
    ts = [D(t) for t in ticks]
    grid = common_tick(ts)
    now = now or datetime(2026, 1, 1, tzinfo=timezone.utc)
    walk = MarketWalk(seed, initial_price)
    out: dict[str, Any] = {"seed": seed, "cycles": cycles, "trade_every_cycles": trade_every_cycles,
                           "levels": "legacy (pre-2C)" if legacy else LEVEL_POLICY,
                           "common_tick": s(grid), "signals": 0, "valid": 0, "invalid": 0,
                           "invalid_by_code": {}, "invalid_trade_ids": [], "scaled": 0,
                           "by_direction": {"LONG": 0, "SHORT": 0}}
    lo = hi = None
    for n in range(cycles):
        price = walk.advance()
        lo = price if lo is None else min(lo, price)
        hi = price if hi is None else max(hi, price)
        if n % trade_every_cycles:
            continue
        long_ = walk.draw_long()
        out["signals"] += 1
        out["by_direction"]["LONG" if long_ else "SHORT"] += 1
        tid = f"soak-{n:06d}"
        try:
            ref = floor_to_step(price, grid)
            lv = legacy_exit_levels(ref, long_) if legacy else exit_levels(ref, long_, grid)
            if lv.scale < 1:
                out["scaled"] += 1
            validate_generated(build_soak_open(now, tid, ref, long_, lv,
                                               max_holding_seconds=max_holding_seconds), ts)
            out["valid"] += 1
        except SoakWorkloadError as e:
            out["invalid"] += 1
            out["invalid_by_code"][e.code] = out["invalid_by_code"].get(e.code, 0) + 1
            out["invalid_trade_ids"].append(tid)
    out["price_min"], out["price_max"] = s(lo), s(hi)
    return out
