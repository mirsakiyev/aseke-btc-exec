"""Executor configuration: per-account policy, routing, and the simulator profiles.

Config is TOML (stdlib ``tomllib``) parsed with Decimal floats. Monetary values
should be written as strings ("100.00"). ``mode`` must be "simulation"; any
request for live mode fails before any order-capable object is constructed.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .domain import InstrumentRules, LevelPolicy, PriceRef, ProductType
from .num import D, s


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class AccountConfig:
    id: str
    venue: str
    strategy_owner: str
    enabled_for_entries: bool
    margin_currency: str
    initial_margin_per_trade: Decimal
    adverse_entry_tolerance: Decimal
    allowed_level_policies: frozenset[LevelPolicy]
    max_spread_fraction: Decimal
    collateral_buffer_fraction: Decimal
    instruments: dict[str, str]  # normalized key ("BTC/USDT:USDT") -> native symbol
    max_initial_margin_hard: Decimal | None = None
    max_notional_hard: Decimal | None = None
    max_loss_at_stop: Decimal | None = None
    source_risk_enforcement: str = "OFF"

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id, "venue": self.venue, "strategy_owner": self.strategy_owner,
            "enabled_for_entries": self.enabled_for_entries,
            "margin_currency": self.margin_currency,
            "initial_margin_per_trade": s(self.initial_margin_per_trade),
            "adverse_entry_tolerance": s(self.adverse_entry_tolerance),
            "allowed_level_policies": sorted(str(p) for p in self.allowed_level_policies),
            "max_spread_fraction": s(self.max_spread_fraction),
            "collateral_buffer_fraction": s(self.collateral_buffer_fraction),
            "instruments": dict(self.instruments),
            "max_initial_margin_hard": s(self.max_initial_margin_hard),
            "max_notional_hard": s(self.max_notional_hard),
            "max_loss_at_stop": s(self.max_loss_at_stop),
            "source_risk_enforcement": self.source_risk_enforcement,
        }


@dataclass(frozen=True)
class SignalPolicy:
    trusted_producers: frozenset[str]
    dummy_producers: frozenset[str]
    max_signal_age_seconds: int = 120
    max_clock_skew_seconds: int = 5
    max_source_data_age_seconds: int = 300
    missing_predecessor_hold_seconds: int = 300
    allowed_currency_mappings: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class ExecutionPolicy:
    max_quote_age_seconds: int = 5
    unknown_resolution_checks: int = 3
    time_exit_overdue_grace_seconds: int = 60


@dataclass(frozen=True)
class SimVenueConfig:
    name: str
    taker_fee_rate: Decimal
    supports_isolated: bool
    supports_cross: bool
    supports_amend: bool
    trigger_references: frozenset[PriceRef]
    instruments: dict[str, InstrumentRules]


@dataclass(frozen=True)
class SimAccountConfig:
    id: str
    venue: str
    currency: str
    starting_balance: Decimal


@dataclass(frozen=True)
class SimConfig:
    db_path: Path
    venues: dict[str, SimVenueConfig]
    accounts: dict[str, SimAccountConfig]
    initial_price: Decimal = Decimal("64500.0")
    initial_depth_btc: Decimal = Decimal("1")


@dataclass(frozen=True)
class Config:
    mode: str
    base_dir: Path
    data_dir: Path
    poll_interval_seconds: float
    reconcile_every_cycles: int
    signals: SignalPolicy
    execution: ExecutionPolicy
    accounts: dict[str, AccountConfig]
    routes: dict[str, tuple[str, ...]]
    sim: SimConfig
    warnings: tuple[str, ...] = ()
    source_hash: str = ""

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.db"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "worker.lock"


# ----------------------------------------------------------------- loading
def load_config(path: str | Path) -> Config:
    p = Path(path).resolve()
    if not p.exists():
        raise ConfigError([f"config file not found: {p}"])
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"), parse_float=Decimal)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError([f"invalid TOML: {e}"]) from None
    return config_from_dict(data, p.parent)


def _canon(o: Any) -> Any:
    if isinstance(o, Decimal):
        return format(o, "f")
    if isinstance(o, Path):
        return str(o)
    raise TypeError(type(o).__name__)


def config_from_dict(data: dict[str, Any], base_dir: Path) -> Config:
    problems: list[str] = []
    warnings: list[str] = []

    def dec(v: Any, where: str, *, allow_none: bool = False, positive: bool = True,
            nonneg: bool = False) -> Decimal | None:
        if v is None or v == "":
            if allow_none:
                return None
            problems.append(f"{where}: required")
            return None
        if isinstance(v, float):
            problems.append(f"{where}: floats not accepted")
            return None
        try:
            d = D(v)
        except (TypeError, ValueError) as e:
            problems.append(f"{where}: {e}")
            return None
        if positive and not nonneg and d <= 0:
            problems.append(f"{where}: must be > 0")
        if nonneg and d < 0:
            problems.append(f"{where}: must be >= 0")
        return d

    mode = str(data.get("mode", "")).lower()
    if mode == "live":
        problems.append("LIVE_NOT_SUPPORTED: mode='live' is not supported in Phase 1; "
                        "simulation is the only executable mode")
    elif mode != "simulation":
        problems.append(f"mode must be 'simulation' (got {data.get('mode')!r})")

    data_dir = (base_dir / str(data.get("data_dir", "var"))).resolve()
    worker = data.get("worker", {}) or {}
    poll = worker.get("poll_interval_seconds", 2)
    poll = float(D(str(poll))) if not isinstance(poll, (int, float)) else float(poll)
    rec_every = int(worker.get("reconcile_every_cycles", 10))

    sig = data.get("signals", {}) or {}
    mappings = frozenset(tuple(m) for m in sig.get("allowed_currency_mappings", []) if len(m) == 2)
    signals = SignalPolicy(
        trusted_producers=frozenset(sig.get("trusted_producers", [])),
        dummy_producers=frozenset(sig.get("dummy_producers", [])),
        max_signal_age_seconds=int(sig.get("max_signal_age_seconds", 120)),
        max_clock_skew_seconds=int(sig.get("max_clock_skew_seconds", 5)),
        max_source_data_age_seconds=int(sig.get("max_source_data_age_seconds", 300)),
        missing_predecessor_hold_seconds=int(sig.get("missing_predecessor_hold_seconds", 300)),
        allowed_currency_mappings=mappings,
    )
    if not signals.trusted_producers:
        problems.append("signals.trusted_producers must list at least one producer")
    for a, b in mappings:
        if a != b:
            warnings.append(f"currency mapping {a}->{b} configured; USD and USDT are NOT "
                            "interchangeable - verify this is intended")

    ex = data.get("execution", {}) or {}
    execution = ExecutionPolicy(
        max_quote_age_seconds=int(ex.get("max_quote_age_seconds", 5)),
        unknown_resolution_checks=max(1, int(ex.get("unknown_resolution_checks", 3))),
        time_exit_overdue_grace_seconds=int(ex.get("time_exit_overdue_grace_seconds", 60)),
    )

    # ---- simulator
    simd = data.get("sim", {}) or {}
    venues: dict[str, SimVenueConfig] = {}
    for vname, vd in (simd.get("venues", {}) or {}).items():
        if not str(vname).startswith("sim_"):
            problems.append(f"sim venue {vname!r} must use a clearly synthetic 'sim_' name")
        instruments: dict[str, InstrumentRules] = {}
        for sym, idf in (vd.get("instruments", {}) or {}).items():
            try:
                pt = ProductType(idf.get("product_type", "LINEAR_PERPETUAL"))
                rules = InstrumentRules(
                    venue=vname, native_symbol=sym, base=idf["base"], quote=idf["quote"],
                    settle=idf["settle"], product_type=pt,
                    contract_multiplier=D(idf["contract_multiplier"]), tick_size=D(idf["tick_size"]),
                    qty_step=D(idf["qty_step"]), min_qty=D(idf["min_qty"]),
                    max_leverage=int(idf["max_leverage"]))
                if min(rules.contract_multiplier, rules.tick_size, rules.qty_step, rules.min_qty) <= 0:
                    raise ValueError("multiplier/tick/step/min must be > 0")
                instruments[sym] = rules
            except (KeyError, ValueError, TypeError) as e:
                problems.append(f"sim.venues.{vname}.instruments.{sym}: {e}")
        refs = frozenset(PriceRef(r) for r in vd.get("trigger_references", ["LAST"]))
        venues[vname] = SimVenueConfig(
            name=vname,
            taker_fee_rate=dec(vd.get("taker_fee_rate", "0"), f"sim.venues.{vname}.taker_fee_rate",
                               nonneg=True) or Decimal(0),
            supports_isolated=bool(vd.get("supports_isolated", True)),
            supports_cross=bool(vd.get("supports_cross", True)),
            supports_amend=bool(vd.get("supports_amend", True)),
            trigger_references=refs, instruments=instruments)
    sim_accounts: dict[str, SimAccountConfig] = {}
    for aid, ad in (simd.get("accounts", {}) or {}).items():
        sim_accounts[aid] = SimAccountConfig(
            id=aid, venue=ad.get("venue", ""), currency=ad.get("currency", "USDT"),
            starting_balance=dec(ad.get("starting_balance"), f"sim.accounts.{aid}.starting_balance")
            or Decimal(0))
    sim = SimConfig(
        db_path=(data_dir / str(simd.get("db_file", "sim_exchange.db"))).resolve(),
        venues=venues, accounts=sim_accounts,
        initial_price=dec(simd.get("initial_price", "64500.0"), "sim.initial_price") or Decimal("64500"),
        initial_depth_btc=dec(simd.get("initial_depth_btc", "1"), "sim.initial_depth_btc") or Decimal(1),
    )

    # ---- accounts
    accounts: dict[str, AccountConfig] = {}
    for ad in data.get("accounts", []) or []:
        aid = str(ad.get("id", ""))
        w = f"accounts[{aid or '?'}]"
        if not aid:
            problems.append("account without id")
            continue
        if aid in accounts:
            problems.append(f"duplicate account id {aid}")
            continue
        if not aid.startswith("sim_"):
            problems.append(f"{w}: Phase 1 account ids must be clearly synthetic ('sim_' prefix)")
        venue = str(ad.get("venue", ""))
        if venue not in venues:
            problems.append(f"{w}: venue {venue!r} is not a configured simulator venue")
        if aid not in sim_accounts:
            problems.append(f"{w}: no [sim.accounts.{aid}] entry")
        elif sim_accounts[aid].venue != venue:
            problems.append(f"{w}: sim account venue mismatch")
        try:
            policies = frozenset(LevelPolicy(p) for p in ad.get("allowed_level_policies", ["ABSOLUTE"]))
        except ValueError as e:
            problems.append(f"{w}: {e}")
            policies = frozenset({LevelPolicy.ABSOLUTE})
        sre = str(ad.get("source_risk_enforcement", "OFF"))
        if sre != "OFF":
            problems.append(f"{w}: source_risk_enforcement={sre!r} is not implemented in Phase 1 "
                            "(only OFF; source risk metadata is informational)")
        for cross_key in ("margin_mode", "allow_cross_fallback"):
            if cross_key in ad and str(ad[cross_key]).upper() not in ("ISOLATED", "FALSE"):
                problems.append(f"{w}: {cross_key} must be ISOLATED/false - cross margin is never used")
        instruments_map = {str(k): str(v) for k, v in (ad.get("instruments", {}) or {}).items()}
        for key, sym in instruments_map.items():
            vcfg = venues.get(venue)
            if vcfg and sym not in vcfg.instruments:
                problems.append(f"{w}: instrument {key} maps to unknown native symbol {sym}")
            elif vcfg:
                r = vcfg.instruments[sym]
                if r.instrument_key != key:
                    problems.append(f"{w}: {key} maps to {sym} whose identity is {r.instrument_key}")
        acc = AccountConfig(
            id=aid, venue=venue, strategy_owner=str(ad.get("strategy_owner", "")),
            enabled_for_entries=bool(ad.get("enabled_for_entries", True)),
            margin_currency=str(ad.get("margin_currency", "")),
            initial_margin_per_trade=dec(ad.get("initial_margin_per_trade"), f"{w}.initial_margin_per_trade") or Decimal(0),
            adverse_entry_tolerance=dec(ad.get("adverse_entry_tolerance"), f"{w}.adverse_entry_tolerance", nonneg=True) or Decimal(0),
            allowed_level_policies=policies,
            max_spread_fraction=dec(ad.get("max_spread_fraction", "0.002"), f"{w}.max_spread_fraction") or Decimal("0.002"),
            collateral_buffer_fraction=dec(ad.get("collateral_buffer_fraction", "0.05"), f"{w}.collateral_buffer_fraction", nonneg=True) or Decimal(0),
            instruments=instruments_map,
            max_initial_margin_hard=dec(ad.get("max_initial_margin_hard"), f"{w}.max_initial_margin_hard", allow_none=True),
            max_notional_hard=dec(ad.get("max_notional_hard"), f"{w}.max_notional_hard", allow_none=True),
            max_loss_at_stop=dec(ad.get("max_loss_at_stop"), f"{w}.max_loss_at_stop", allow_none=True),
            source_risk_enforcement=sre,
        )
        if not acc.strategy_owner:
            problems.append(f"{w}: strategy_owner is required (one strategy owner per account)")
        if not acc.margin_currency:
            problems.append(f"{w}: margin_currency is required")
        accounts[aid] = acc

    routes: dict[str, tuple[str, ...]] = {}
    for rd in data.get("routes", []) or []:
        sid = str(rd.get("strategy_id", ""))
        accs = tuple(str(a) for a in rd.get("accounts", []))
        if not sid:
            problems.append("route without strategy_id")
            continue
        for a in accs:
            if a not in accounts:
                problems.append(f"route {sid}: unknown account {a}")
            elif accounts[a].strategy_owner != sid:
                warnings.append(f"route {sid} lists account {a} owned by "
                                f"{accounts[a].strategy_owner}; entries there will be rejected "
                                "(STRATEGY_NOT_OWNER)")
        routes[sid] = accs

    if problems:
        raise ConfigError(problems)
    src_hash = hashlib.sha256(json.dumps(data, sort_keys=True, default=_canon).encode()).hexdigest()
    return Config(mode="simulation", base_dir=base_dir, data_dir=data_dir,
                  poll_interval_seconds=poll, reconcile_every_cycles=max(1, rec_every),
                  signals=signals, execution=execution, accounts=accounts, routes=routes,
                  sim=sim, warnings=tuple(warnings), source_hash=src_hash)
