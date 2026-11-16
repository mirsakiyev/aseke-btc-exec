"""Deterministic acceptance scenarios (shared by `aseke-exec scenario run` and pytest).

Every scenario runs in its own directory with an injected ManualClock, a fresh
persistent simulator and journal, and asserts business invariants about orders,
exposure, protection, persisted state and accounting. No network, no real keys.
Timestamps are generated from the scenario clock (fixture start below).
"""

from __future__ import annotations

import copy
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .clock import UTC, ManualClock
from .config import ConfigError, config_from_dict
from .engine import Engine
from .examples import build_amend, build_cancel, build_close, build_open
from .inbox import Inbox, publish_event
from .journal import Journal
from .lock import LockHeld, ProcessLock
from .num import D
from .sim import SimAdapter, SimExchange

FIXTURE_START = datetime(2026, 1, 5, 12, 0, 0, tzinfo=UTC)  # scenario fixture time
STRATEGY = "lab_btc_breakout"
A, B = "sim_alpha_a1", "sim_beta_b1"
SYM = {A: "BTCUSDT-ALPHA", B: "BTCUSDT-BETA"}
VENUE = {A: "sim_alpha", B: "sim_beta"}


def default_config_dict() -> dict[str, Any]:
    """Mirror of config/sim.toml (kept in sync by tests)."""
    return {
        "mode": "simulation",
        "data_dir": "var",
        "worker": {"poll_interval_seconds": 2, "reconcile_every_cycles": 10},
        "signals": {"trusted_producers": ["aseke-btc-lab", "aseke-dummy-publisher"],
                    "dummy_producers": ["aseke-dummy-publisher"],
                    "max_signal_age_seconds": 120, "max_clock_skew_seconds": 5,
                    "max_source_data_age_seconds": 300, "missing_predecessor_hold_seconds": 300,
                    "allowed_currency_mappings": []},
        "execution": {"max_quote_age_seconds": 5, "unknown_resolution_checks": 3,
                      "time_exit_overdue_grace_seconds": 60},
        "accounts": [
            {"id": A, "venue": "sim_alpha", "strategy_owner": STRATEGY, "enabled_for_entries": True,
             "margin_currency": "USDT", "initial_margin_per_trade": "100.00",
             "adverse_entry_tolerance": "100.00", "allowed_level_policies": ["ABSOLUTE"],
             "max_spread_fraction": "0.002", "collateral_buffer_fraction": "0.05",
             "max_initial_margin_hard": "110.00", "max_notional_hard": "600.00",
             "instruments": {"BTC/USDT:USDT": "BTCUSDT-ALPHA"}},
            {"id": B, "venue": "sim_beta", "strategy_owner": STRATEGY, "enabled_for_entries": True,
             "margin_currency": "USDT", "initial_margin_per_trade": "100.00",
             "adverse_entry_tolerance": "100.00", "allowed_level_policies": ["ABSOLUTE"],
             "max_spread_fraction": "0.002", "collateral_buffer_fraction": "0.05",
             "max_initial_margin_hard": "110.00", "max_notional_hard": "600.00",
             "instruments": {"BTC/USDT:USDT": "BTCUSDT-BETA"}},
        ],
        "routes": [{"strategy_id": STRATEGY, "accounts": [A, B]}],
        "sim": {
            "db_file": "sim_exchange.db", "initial_price": "64500.0", "initial_depth_btc": "1",
            "venues": {
                "sim_alpha": {"taker_fee_rate": "0.0006", "supports_isolated": True,
                              "supports_cross": True, "supports_amend": True,
                              "trigger_references": ["LAST", "MARK", "INDEX"],
                              "instruments": {"BTCUSDT-ALPHA": {
                                  "base": "BTC", "quote": "USDT", "settle": "USDT",
                                  "product_type": "LINEAR_PERPETUAL", "contract_multiplier": "0.001",
                                  "tick_size": "0.1", "qty_step": "1", "min_qty": "1",
                                  "max_leverage": 100}}},
                "sim_beta": {"taker_fee_rate": "0.0005", "supports_isolated": True,
                             "supports_cross": True, "supports_amend": False,
                             "trigger_references": ["LAST", "MARK"],
                             "instruments": {"BTCUSDT-BETA": {
                                 "base": "BTC", "quote": "USDT", "settle": "USDT",
                                 "product_type": "LINEAR_PERPETUAL", "contract_multiplier": "0.0001",
                                 "tick_size": "0.05", "qty_step": "5", "min_qty": "10",
                                 "max_leverage": 50}}},
            },
            "accounts": {A: {"venue": "sim_alpha", "currency": "USDT", "starting_balance": "1000.00"},
                         B: {"venue": "sim_beta", "currency": "USDT", "starting_balance": "1000.00"}},
        },
    }


class ScenarioFailure(AssertionError):
    pass


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise ScenarioFailure(msg)


class Harness:
    def __init__(self, root: str | Path, mutate: Callable[[dict[str, Any]], None] | None = None,
                 start: datetime = FIXTURE_START):
        self.root = Path(root)
        self.clock = ManualClock(start)
        self.cfg_dict = default_config_dict()
        if mutate:
            mutate(self.cfg_dict)
        self.cfg = config_from_dict(self.cfg_dict, self.root)
        self.sim = SimExchange(self.cfg.sim.db_path, self.clock)
        self.sim.install(self.cfg.sim)
        self.journal: Journal | None = None
        self.engine: Engine | None = None
        self.alerts: list[dict[str, Any]] = []
        self._n = 0

    # ---- worker lifecycle
    def start(self) -> Engine:
        self.journal = Journal(self.cfg.journal_path)
        adapters = {aid: SimAdapter(self.sim, a.venue, aid) for aid, a in self.cfg.accounts.items()}
        self.engine = Engine(self.cfg, self.journal, adapters, self.clock, Inbox(self.cfg.inbox_dir),
                             alert_sink=self.alerts.append)
        self.engine.startup_reconcile()
        return self.engine

    def crash(self) -> None:
        """Abrupt stop: no cleanup; the simulator keeps its own persistent state."""
        assert self.journal is not None
        self.journal.close()
        self.journal = None
        self.engine = None

    def reconfigure(self, mutate: Callable[[dict[str, Any]], None]) -> None:
        d = copy.deepcopy(self.cfg_dict)
        mutate(d)
        cfg = config_from_dict(d, self.root)
        assert self.engine is not None
        self.engine.apply_config(cfg)
        self.cfg_dict, self.cfg = d, cfg

    def cycle(self, n: int = 1) -> None:
        assert self.engine is not None
        for _ in range(n):
            self.engine.run_cycle()

    # ---- signals and market
    def publish(self, ev: dict[str, Any], filename: str | None = None) -> Path:
        self._n += 1
        return publish_event(self.cfg.inbox_dir, ev, filename or f"{self._n:04d}_{ev['event_id']}.json")

    def now(self) -> datetime:
        return self.clock.now()

    def market(self, price: str, accounts: tuple[str, ...] = (A, B), **kw: Any) -> None:
        for a in accounts:
            self.sim.set_simple_market(VENUE[a], SYM[a], price, **kw)

    # ---- inspection
    @property
    def j(self) -> Journal:
        assert self.journal is not None
        return self.journal

    def ex(self, trade_id: str, account: str) -> dict[str, Any]:
        r = self.j.q1("SELECT * FROM executions WHERE trade_id=? AND account_id=?", (trade_id, account))
        check(r is not None, f"no execution for {trade_id}/{account}")
        return r  # type: ignore[return-value]

    def has_ex(self, trade_id: str, account: str) -> bool:
        return self.j.q1("SELECT id FROM executions WHERE trade_id=? AND account_id=?",
                         (trade_id, account)) is not None

    def pos(self, account: str) -> dict[str, Any]:
        return self.sim.position(VENUE[account], account, SYM[account])

    def venue_orders(self, account: str, kind: str | None = None, status: str | None = None) -> list[Any]:
        rows = self.sim.all_orders(VENUE[account], account)
        return [r for r in rows if (kind is None or r["kind"] == kind) and
                (status is None or r["status"] == status)]

    def event(self, event_id: str) -> dict[str, Any]:
        r = self.j.event_by_id(event_id)
        check(r is not None, f"event {event_id} not journaled")
        return r  # type: ignore[return-value]

    def assert_flat_and_clean(self, account: str) -> None:
        p = self.pos(account)
        check(p["qty"] == 0, f"{account} not flat: {p}")
        check(not self.venue_orders(account, status="OPEN"), f"{account} has orphan working orders")


# =============================================================== scenarios
def sc_long_reference_two_venues(h: Harness) -> dict[str, Any]:
    """Reference LONG routed to two venues with different multipliers; TP exit."""
    h.start()
    h.publish(build_open(h.now(), "t-long"))
    h.cycle()
    a, b = h.ex("t-long", A), h.ex("t-long", B)
    for e in (a, b):
        check(e["state"] == "OPEN_PROTECTED", f"{e['account_id']} state {e['state']}")
        check(e["limit_price"] == D("64600.1"), f"ceiling {e['limit_price']}")
        check(e["target_notional"] == D("500.00"), "target notional must be M*L = 500")
        check(e["planned_notional"] <= D("500"), "planned notional must not exceed M*L at the bound")
        check(e["exit_plan"]["stop"] in ("64000.10", "64000.1") and D(e["exit_plan"]["stop"]) == D("64000.10"),
              "absolute stop must be unchanged")
        check(D(e["exit_plan"]["legs"][0]["price"]) == D("66000.10"), "absolute TP unchanged")
        check(e["protected_qty"] == e["filled_qty"], "filled qty must be fully protected")
    check(a["planned_qty"] == D(7) and b["planned_qty"] == D(75), f"contracts {a['planned_qty']}/{b['planned_qty']}")
    # economic exposure comparable despite different native contract counts
    check(a["filled_qty_btc"] == D("0.007") and b["filled_qty_btc"] == D("0.0075"), "BTC exposure")
    check(abs(a["filled_qty_btc"] - b["filled_qty_btc"]) <= D("0.001"), "exposure within one alpha lot")
    stop_a = h.venue_orders(A, "STOP_MARKET", "OPEN")
    check(len(stop_a) == 1 and D(stop_a[0]["qty"]) == 7 and D(stop_a[0]["trigger_price"]) == D("64000.1"),
          "alpha stop must cover 7 @ 64000.1")
    check(a["risk_at_stop"] == D("0.007") * (D("64500.1") - D("64000.10")), f"risk at stop {a['risk_at_stop']}")
    h.market("66100")  # TP triggers on the exchange
    h.cycle()
    for acct in (A, B):
        e = h.ex("t-long", acct)
        check(e["state"] == "CLOSED" and e["close_reason"] == "TAKE_PROFIT", f"{acct} {e['state']} {e['close_reason']}")
        check(e["realized_pnl"] > 0, "TP exit should realize a gain")
        h.assert_flat_and_clean(acct)
    return {"alpha_contracts": "7", "beta_contracts": "75", "alpha_btc": "0.007", "beta_btc": "0.0075"}


def sc_short_reference_stop(h: Harness) -> dict[str, Any]:
    h.start()
    h.publish(build_open(h.now(), "t-short", direction="SHORT"))
    h.cycle()
    a = h.ex("t-short", A)
    check(a["limit_price"] == D("64400.1"), f"SHORT floor {a['limit_price']}")
    check(a["state"] == "OPEN_PROTECTED", a["state"])
    check(D(a["exit_plan"]["stop"]) == D("65000.10"), "stop unchanged")
    check(h.pos(A)["side"] == "SHORT" and h.pos(A)["qty"] == 7, "alpha short 7")
    check(abs(a["risk_at_stop"] - D("0.007") * (D("65000.1") - a["avg_fill"])) == 0, "risk formula")
    h.market("65100")
    h.cycle()
    for acct in (A, B):
        e = h.ex("t-short", acct)
        check(e["state"] == "CLOSED" and e["close_reason"] == "STOP_LOSS", f"{acct} {e['close_reason']}")
        check(e["realized_pnl"] < 0, "stop loss realizes a loss")
        h.assert_flat_and_clean(acct)
    return {"short_floor": "64400.1"}


def sc_price_eligibility(h: Harness) -> dict[str, Any]:
    h.start()
    h.market("64650")  # ask 64650.1 > ceiling 64600.1
    h.publish(build_open(h.now(), "t-adverse"))
    h.cycle()
    check(h.ex("t-adverse", A)["reason_code"] == "ENTRY_PRICE_ADVERSE", "adverse must reject")
    h.market("63900")  # favourable by 600 but through the absolute stop
    h.publish(build_open(h.now(), "t-through-stop"))
    h.cycle()
    e = h.ex("t-through-stop", A)
    check(e["reason_code"] == "STOP_ALREADY_BREACHED", e["reason_code"])
    check(D(e["exit_spec"]["stop"]["price"]) == D("64000.10"), "stop must not be moved")
    check(not h.venue_orders(A, "ENTRY_LIMIT_IOC"), "no entry may be sent for rejected setups")
    h.market("64300")  # favourable by 200 (> tolerance 100) and still valid
    h.publish(build_open(h.now(), "t-favourable"))
    h.cycle()
    f = h.ex("t-favourable", A)
    check(f["state"] == "OPEN_PROTECTED", f"favourable entry should be accepted: {f['reason_code']}")
    check(f["avg_fill"] == D("64300.1"), f"fill {f['avg_fill']}")
    check(D(f["exit_plan"]["stop"]) == D("64000.10"), "stop unchanged after favourable fill")
    return {"adverse": "rejected", "through_stop": "rejected", "favourable_200": "accepted"}


def sc_preflight_rejections(mk: Callable[..., Harness]) -> dict[str, Any]:
    def mutate(d: dict[str, Any]) -> None:
        d["sim"]["venues"]["sim_gamma"] = {
            "taker_fee_rate": "0.0005", "supports_isolated": False, "supports_cross": True,
            "trigger_references": ["LAST"], "instruments": {"BTCUSDT-GAMMA": dict(
                d["sim"]["venues"]["sim_alpha"]["instruments"]["BTCUSDT-ALPHA"])}}
        d["sim"]["accounts"]["sim_gamma_g1"] = {"venue": "sim_gamma", "currency": "USDT",
                                                "starting_balance": "1000"}
        g = dict(d["accounts"][0], id="sim_gamma_g1", venue="sim_gamma", strategy_owner="lab_other",
                 instruments={"BTC/USDT:USDT": "BTCUSDT-GAMMA"})
        d["accounts"].append(g)
        d["routes"].append({"strategy_id": "lab_other", "accounts": ["sim_gamma_g1", A]})
        d["accounts"][0]["max_notional_hard"] = "10000.00"  # allow 75x x 100 margin on alpha
        d["accounts"][0]["max_initial_margin_hard"] = "110.00"
    h = mk(mutate)
    h.start()
    h.publish(build_open(h.now(), "t-lev75", leverage=75))
    h.cycle()
    check(h.ex("t-lev75", A)["state"] == "OPEN_PROTECTED", "alpha supports 75x")
    check(h.ex("t-lev75", B)["reason_code"] == "LEVERAGE_UNSUPPORTED", "beta max 50x: reject, never reduce")
    check(h.ex("t-lev75", A)["leverage"] == 75, "leverage used exactly")
    h.publish(build_open(h.now(), "t-overlap"))
    h.sim.set_wallet("sim_beta", B, "10")
    h.cycle()
    check(h.ex("t-overlap", A)["reason_code"] == "OVERLAPPING_TRADE", "overlap rejected")
    check(h.ex("t-overlap", B)["reason_code"] == "INSUFFICIENT_COLLATERAL", "collateral rejected")
    h.publish(build_open(h.now(), "t-other", strategy_id="lab_other"))
    h.cycle()
    check(h.ex("t-other", "sim_gamma_g1")["reason_code"] == "MARGIN_MODE_UNSUPPORTED", "cross-only venue")
    check(h.ex("t-other", A)["reason_code"] == "STRATEGY_NOT_OWNER", "wrong owner rejected")
    check(not h.venue_orders(B, "ENTRY_LIMIT_IOC"), "no beta entry orders sent")
    return {"rejections": "LEVERAGE_UNSUPPORTED, OVERLAPPING_TRADE, INSUFFICIENT_COLLATERAL, "
                          "MARGIN_MODE_UNSUPPORTED, STRATEGY_NOT_OWNER"}


def sc_entry_relative(mk: Callable[..., Harness]) -> dict[str, Any]:
    def mutate(d: dict[str, Any]) -> None:
        d["accounts"][0]["allowed_level_policies"] = ["ABSOLUTE", "ENTRY_RELATIVE"]
    h = mk(mutate)
    h.start()
    h.publish(build_open(h.now(), "t-rel-long", level_policy="ENTRY_RELATIVE", stop="0.01",
                         take_profits=[("0.02", "1")]))
    h.cycle()
    a = h.ex("t-rel-long", A)
    check(a["avg_fill"] == D("64500.1"), "anchor = actual VWAP fill")
    check(D(a["exit_plan"]["stop"]) == D("63855.1"), f"LONG rel stop {a['exit_plan']['stop']}")
    check(D(a["exit_plan"]["legs"][0]["price"]) == D("65790.1"), "LONG rel TP")
    check(h.ex("t-rel-long", B)["reason_code"] == "LEVEL_POLICY_NOT_PERMITTED", "beta allows ABSOLUTE only")
    h.publish(build_close(h.now(), "t-rel-long", 2))
    h.cycle()
    check(h.ex("t-rel-long", A)["state"] == "CLOSED", "closed")
    h.market("64500")
    h.publish(build_open(h.now(), "t-rel-short", direction="SHORT", level_policy="ENTRY_RELATIVE",
                         stop="0.01", take_profits=[("0.02", "1")]))
    h.cycle()
    s_ = h.ex("t-rel-short", A)
    check(s_["avg_fill"] == D("64499.9"), f"short fill {s_['avg_fill']}")
    check(D(s_["exit_plan"]["stop"]) == D("65144.8"), f"SHORT rel stop {s_['exit_plan']['stop']}")
    check(D(s_["exit_plan"]["legs"][0]["price"]) == D("63210.0"), f"SHORT rel TP {s_['exit_plan']['legs']}")
    return {"long_stop": "63855.1", "long_tp": "65790.1", "short_stop": "65144.8", "short_tp": "63210.0"}


def sc_source_risk_informational(h: Harness) -> dict[str, Any]:
    h.start()
    h.publish(build_open(h.now(), "t-risk", source_risk={"risk_fraction": "0.002",
                                                         "basis": "SOURCE_PAPER_EQUITY",
                                                         "note": "informal 0.2% strategy risk"}))
    h.cycle()
    a = h.ex("t-risk", A)
    check(a["state"] == "OPEN_PROTECTED", "accepted")
    check(D(a["exit_plan"]["stop"]) == D("64000.10"), "0.2% must not redefine the stop")
    check(a["planned_qty"] == 7, "0.2% must not change sizing (margin x leverage only)")
    check(a["source_risk"]["risk_fraction"] == "0.002", "stored as informational metadata")
    expected_risk = D("0.007") * (a["avg_fill"] - D("64000.10"))
    check(a["risk_at_stop"] == expected_risk, "reported risk is level-based")
    check(a["risk_pct_of_margin"] == expected_risk / a["actual_margin"] * 100, "pct of actual margin")
    return {"risk_at_stop": str(a["risk_at_stop"]), "risk_pct_of_margin": f"{a['risk_pct_of_margin']:.3f}"}


def sc_ioc_outcomes(h: Harness) -> dict[str, Any]:
    h.start()
    # partial fill on alpha: only 3 contracts inside the bound
    h.sim.set_market("sim_alpha", SYM[A], asks=[("64500.1", "3"), ("64700.0", "1000")])
    h.publish(build_open(h.now(), "t-partial"))
    h.cycle(3)
    a = h.ex("t-partial", A)
    check(a["entry_outcome"] == "PARTIAL_FILL" and a["filled_qty"] == 3, f"{a['entry_outcome']} {a['filled_qty']}")
    check(a["state"] == "OPEN_PROTECTED" and a["protected_qty"] == 3, "partial must be protected")
    check(len(h.venue_orders(A, "ENTRY_LIMIT_IOC")) == 1, "never chase/top-up the residual")
    check(a["unused_allocation"] == D("100.00") - a["actual_margin"], "unused allocation recorded")
    stop = h.venue_orders(A, "STOP_MARKET", "OPEN")[0]
    check(D(stop["qty"]) == 3, "stop covers actual filled qty")
    # close it, then no-fill / venue-reject
    h.publish(build_close(h.now(), "t-partial", 2))
    h.cycle()
    h.market("64500")
    h.sim.add_fault("sim_alpha", "place_entry", "NO_FILL", account=A)
    h.sim.add_fault("sim_beta", "place_entry", "REJECT", account=B, param="SIM_RISK_LIMIT")
    h.publish(build_open(h.now(), "t-nofill"))
    h.cycle()
    n = h.ex("t-nofill", A)
    check(n["state"] == "ENTRY_NOT_FILLED" and n["entry_outcome"] == "NO_FILL", n["state"])
    check(n["reserved_amount"] == 0 and h.pos(A)["qty"] == 0, "reservation released, flat")
    r = h.ex("t-nofill", B)
    check(r["state"] == "ENTRY_VENUE_REJECTED", r["state"])
    return {"partial": "3/7 protected", "no_fill": "ENTRY_NOT_FILLED", "venue_reject": "ENTRY_VENUE_REJECTED"}


def sc_partial_infeasible_emergency(h: Harness) -> dict[str, Any]:
    h.start()
    h.sim.set_market("sim_alpha", SYM[A], asks=[("64500.1", "1"), ("64700.0", "1000")])
    h.publish(build_open(h.now(), "t-inf", take_profits=[("65000.10", "0.5"), ("66000.10", "0.5")]))
    h.cycle()
    a = h.ex("t-inf", A)
    check(a["state"] == "CLOSED" and a["close_reason"] == "EMERGENCY_EXIT_PLAN_INFEASIBLE",
          f"{a['state']} {a['close_reason']}")
    h.assert_flat_and_clean(A)
    b = h.ex("t-inf", B)
    check(b["state"] == "OPEN_PROTECTED", "beta unaffected")
    legs = [D(l["qty"]) for l in b["exit_plan"]["legs"]]
    check(legs == [D(35), D(40)], f"beta TP allocation {legs} (floor, last absorbs residual)")
    return {"alpha": "1 contract cannot carry 2 TP legs -> emergency close", "beta_legs": "35/40"}


def sc_entry_timeout_after_accept_recovery(h: Harness) -> dict[str, Any]:
    h.start()
    h.sim.add_fault("sim_alpha", "place_entry", "TIMEOUT_AFTER_ACCEPT", account=A)
    h.sim.add_fault("sim_alpha", "get_order", "STALE_NOT_FOUND", account=A)  # delayed visibility
    h.publish(build_open(h.now(), "t-unk"))
    h.cycle()
    check(h.ex("t-unk", A)["state"] == "ENTRY_UNKNOWN", "timeout -> UNKNOWN")
    check(h.ex("t-unk", B)["state"] == "OPEN_PROTECTED", "beta independent")
    h.crash()
    h.clock.advance(30)
    h.market("64800")
    h.start()  # startup reconciliation recovers from independently persisted venue state
    a = h.ex("t-unk", A)
    check(a["state"] == "OPEN_PROTECTED" and a["filled_qty"] == 7, f"recovered {a['state']}")
    check(len(h.venue_orders(A, "ENTRY_LIMIT_IOC")) == 1, "no second entry while ambiguous")
    check(not h.j.open_incidents(A), "recovered exposure is owned, not an incident")
    return {"recovered": "fill discovered after restart; protected"}


def sc_entry_timeout_before_accept(h: Harness) -> dict[str, Any]:
    h.start()
    h.sim.add_fault("sim_alpha", "place_entry", "TIMEOUT_BEFORE_ACCEPT", account=A)
    h.publish(build_open(h.now(), "t-lost"))
    h.cycle()
    check(h.ex("t-lost", A)["state"] == "ENTRY_UNKNOWN", "UNKNOWN")
    h.cycle(3)
    a = h.ex("t-lost", A)
    check(a["state"] == "ENTRY_NOT_ACCEPTED", a["state"])
    check(not h.venue_orders(A, "ENTRY_LIMIT_IOC") and h.pos(A)["qty"] == 0, "nothing on venue")
    n = h.j.q1("SELECT COUNT(*) AS n FROM orders WHERE execution_id=? AND leg='ENTRY'", (a["id"],))
    check(n["n"] == 1, "never resubmitted")  # type: ignore[index]
    return {"resolved": "ENTRY_NOT_ACCEPTED after bounded checks"}


def sc_protection_failure(h: Harness) -> dict[str, Any]:
    h.start()
    h.sim.add_fault("sim_beta", "place_protective", "REJECT", account=B, param="SIM_TRIGGER_REJECT")
    h.publish(build_open(h.now(), "t-pf"))
    h.cycle()
    b = h.ex("t-pf", B)
    check(b["state"] == "CLOSED" and b["close_reason"] == "EMERGENCY_PROTECTION_FAILED", f"{b['state']}")
    h.assert_flat_and_clean(B)
    acct = h.j.account(B)
    check(acct["entries_blocked"] == 1, "affected account blocked")  # type: ignore[index]
    inc = h.j.q("SELECT * FROM incidents WHERE account_id=? AND kind='PROTECTION_FAILED'", (B,))
    check(len(inc) == 1 and inc[0]["status"] == "RESOLVED", "incident durable; resolved once flat")
    check(h.ex("t-pf", A)["state"] == "OPEN_PROTECTED", "healthy account keeps its protected position")
    h.publish(build_open(h.now(), "t-pf2"))
    h.cycle()
    check(h.ex("t-pf2", B)["reason_code"] == "ACCOUNT_BLOCKED", "blocked account takes no new entries")
    return {"beta": "emergency closed + blocked", "alpha": "still protected"}


def sc_close_paths(h: Harness) -> dict[str, Any]:
    h.start()
    h.publish(build_open(h.now(), "t-close"))
    h.cycle()
    h.sim.add_fault("sim_alpha", "place_close", "TIMEOUT_AFTER_ACCEPT", account=A)
    h.sim.add_fault("sim_beta", "place_close", "PARTIAL", account=B, param="0.5")
    h.publish(build_close(h.now(), "t-close", 2))
    h.cycle()
    closes_b = h.venue_orders(B, "CLOSE_MARKET")
    check(len(closes_b) == 2 and D(closes_b[0]["filled_qty"]) == 35 and closes_b[0]["status"] == "CANCELLED",
          "first close partially filled (35/75); remaining 40 closed by a second attempt")
    check(len(h.venue_orders(A, "CLOSE_MARKET")) == 1, "timed-out close reconciled, not resent")
    h.cycle()
    for acct in (A, B):
        e = h.ex("t-close", acct)
        check(e["state"] == "CLOSED" and e["close_reason"] == "LAB_CLOSE", f"{acct} {e['state']}")
        h.assert_flat_and_clean(acct)
        exit_qty = sum((f["qty"] for f in h.j.fills_for(e["id"], ("STOP", "TP", "CLOSE"))), D(0))
        check(exit_qty == e["filled_qty"], "exit qty equals entry qty (no reversal)")
    closes_before = len(h.venue_orders(A, "CLOSE_MARKET"))
    h.publish(build_close(h.now(), "t-close", 3, event_id="t-close-r3-close-again"))
    h.cycle()
    check(len(h.venue_orders(A, "CLOSE_MARKET")) == closes_before, "repeated CLOSE has no effect")
    return {"timeout_close": "reconciled", "partial_close": "continued to flat"}


def sc_downtime_stop_and_deadline(h: Harness) -> dict[str, Any]:
    h.start()
    h.publish(build_open(h.now(), "t-down", holding={"max_holding_seconds": 600}))
    h.cycle()
    deadline = h.ex("t-down", B)["deadline_at"]
    check(deadline is not None, "deadline from first fill")
    h.crash()
    h.market("63900", accounts=(A,))  # alpha's exchange-held stop fires while worker is down
    h.clock.advance(900)  # beta's holding deadline passes during the outage
    h.start()
    a, b = h.ex("t-down", A), h.ex("t-down", B)
    check(a["state"] == "CLOSED" and a["close_reason"] == "STOP_LOSS", f"alpha {a['close_reason']}")
    check(b["state"] == "CLOSED" and b["close_reason"] == "TIME_EXIT", f"beta {b['close_reason']}")
    check(any(al["code"] == "TIME_EXIT_OVERDUE" for al in h.alerts), "overdue time exit alerted")
    for acct in (A, B):
        h.assert_flat_and_clean(acct)
    return {"alpha": "stop discovered after restart", "beta": "overdue time exit handled"}


def sc_event_ordering(h: Harness) -> dict[str, Any]:
    h.start()
    ev = build_open(h.now(), "t-ord")
    h.publish(ev, "a_open.json")
    h.publish(ev, "b_open_duplicate.json")  # same identity + content
    bad = dict(ev, open=dict(ev["open"], leverage=6))
    h.publish(bad, "c_open_conflict.json")  # same identity, different content
    (h.cfg.inbox_dir / ".d_partial.json.tmp").write_text('{"schema_version": "aseke.ex', encoding="utf-8")
    h.cycle()
    check(h.j.q1("SELECT COUNT(*) AS n FROM executions WHERE trade_id='t-ord'")["n"] == 2,  # type: ignore[index]
          "duplicate file must not create extra executions")
    outcomes = {r["file_name"]: r["outcome"] for r in h.j.q("SELECT * FROM ingest_log")}
    check(outcomes.get("b_open_duplicate.json") == "DUPLICATE", "duplicate detected")
    check(outcomes.get("c_open_conflict.json") == "CONFLICT", "conflict detected")
    check((h.cfg.inbox_dir.parent / "quarantine" / "c_open_conflict.json").exists(), "conflict quarantined")
    check((h.cfg.inbox_dir / ".d_partial.json.tmp").exists(), "incomplete temp file ignored")
    check(len(h.venue_orders(A, "ENTRY_LIMIT_IOC")) == 1, "exactly one entry on the venue")
    # AMEND r3 then stale r2
    h.market("64500")
    h.publish(build_amend(h.now(), "t-ord", 3, stop="64100.10", take_profits=[("66000.10", "1")]))
    h.publish(build_amend(h.now(), "t-ord", 2, stop="63000.10", take_profits=[("66000.10", "1")]))
    h.cycle()
    check(D(h.ex("t-ord", A)["exit_plan"]["stop"]) == D("64100.10"), "newer amendment kept")
    check(h.event("t-ord-r2-amend_exits")["process_status"] == "STALE", "stale amendment ignored")
    # CLOSE before OPEN -> tombstone
    h.publish(build_close(h.now(), "t-tomb", 2))
    h.cycle()
    h.publish(build_open(h.now(), "t-tomb"))
    h.cycle()
    check(h.event("t-tomb-r1-open")["process_code"] == "TRADE_TOMBSTONED", "delayed OPEN refused")
    check(not h.has_ex("t-tomb", A), "no exposure from a tombstoned trade")
    # AMEND with missing predecessor: bounded hold then reject
    h.publish(build_amend(h.now(), "t-orphan", 2, stop="64000.10", take_profits=[("66000.10", "1")]))
    h.cycle()
    check(h.event("t-orphan-r2-amend_exits")["process_status"] == "DEFERRED", "held")
    h.clock.advance(301)
    h.cycle()
    check(h.event("t-orphan-r2-amend_exits")["process_code"] == "MISSING_PREDECESSOR", "rejected after hold")
    return {"duplicates": "no second effect", "stale": "ignored", "tombstone": "OPEN refused"}


def sc_crash_around_ingestion(h: Harness) -> dict[str, Any]:
    eng = h.start()
    ev = build_open(h.now(), "t-crash")
    path = h.publish(ev, "x_open.json")
    eng.ingest_text(path.read_text(encoding="utf-8"), path.name, None)  # journaled, not archived
    h.crash()  # crash before archive and before processing
    h.start()
    h.cycle()
    check(h.j.q1("SELECT COUNT(*) AS n FROM events WHERE event_id=?", (ev["event_id"],))["n"] == 1,  # type: ignore[index]
          "event journaled exactly once")
    check(not path.exists(), "file archived on recovery scan")
    check(h.ex("t-crash", A)["state"] == "OPEN_PROTECTED", "processed exactly once")
    check(len(h.venue_orders(A, "ENTRY_LIMIT_IOC")) == 1, "single entry")
    return {"recovery": "duplicate file archived, one entry"}


def sc_multi_tp_and_competing_exits(h: Harness) -> dict[str, Any]:
    h.start()
    h.publish(build_open(h.now(), "t-mtp", take_profits=[("65000.10", "0.5"), ("66000.10", "0.5")]))
    h.cycle()
    a = h.ex("t-mtp", A)
    check([D(l["qty"]) for l in a["exit_plan"]["legs"]] == [D(3), D(4)], "alpha legs 3/4")
    h.market("65100")  # TP1 fills on both venues
    h.cycle()
    check(h.pos(A)["qty"] == 4 and h.pos(B)["qty"] == 40, f"residual {h.pos(A)['qty']}/{h.pos(B)['qty']}")
    for acct, q in ((A, 4), (B, 40)):
        stops = h.venue_orders(acct, "STOP_MARKET", "OPEN")
        check(len(stops) == 1 and D(stops[0]["qty"]) == q, f"{acct} residual stop coverage {stops}")
    h.market("63900")  # stop takes the rest
    h.cycle()
    for acct in (A, B):
        e = h.ex("t-mtp", acct)
        check(e["state"] == "CLOSED" and e["close_reason"] == "STOP_LOSS", f"{acct} {e['close_reason']}")
        h.assert_flat_and_clean(acct)
    # competing: TPs fill during downtime while a CLOSE is waiting
    h.market("64500")
    h.publish(build_open(h.now(), "t-race", take_profits=[("65000.10", "0.5"), ("66000.10", "0.5")]))
    h.cycle()
    h.crash()
    h.market("66100")
    h.publish(build_close(h.now(), "t-race", 2))
    h.start()
    h.cycle()
    for acct in (A, B):
        e = h.ex("t-race", acct)
        check(e["state"] == "CLOSED", f"{acct} {e['state']}")
        check(e["close_reason"] == "TAKE_PROFIT", f"actual exit was TP: {e['close_reason']}")
        check(not h.venue_orders(acct, "CLOSE_MARKET"), "no close sent once flat (cannot reverse)")
        h.assert_flat_and_clean(acct)
    return {"partial_tp": "stop resized (alpha amend, beta replace)", "race": "no reversal"}


def sc_pause_and_disable(h: Harness) -> dict[str, Any]:
    h.start()
    h.publish(build_open(h.now(), "t-p1"))
    h.cycle()
    h.j.set_control("pause_new_entries", "1", "test", h.now())
    h.publish(build_open(h.now(), "t-p2"))
    h.market("66100")  # existing protection must still work while paused
    h.cycle()
    check(h.ex("t-p2", A)["reason_code"] == "ENTRIES_PAUSED", "paused: no new entries")
    check(h.ex("t-p1", A)["state"] == "CLOSED", "existing exposure still managed while paused")
    h.j.set_control("pause_new_entries", "0", "test", h.now())
    h.j.update("accounts", "account_id", B, operator_disabled=1, updated_at=h.now())
    h.market("64500")
    h.publish(build_open(h.now(), "t-p3", holding={"max_holding_seconds": 60}))
    h.cycle()
    check(h.ex("t-p3", A)["state"] == "OPEN_PROTECTED", "alpha entered")
    check(h.ex("t-p3", B)["reason_code"] == "ACCOUNT_DISABLED", "beta disabled for entries")
    h.j.update("accounts", "account_id", A, operator_disabled=1, updated_at=h.now())
    h.clock.advance(61)
    h.cycle()
    check(h.ex("t-p3", A)["close_reason"] == "TIME_EXIT", "disabled account still runs exits")
    return {"pause": "entries skipped, exits managed", "disable": "same"}


def sc_config_change_routing(mk: Callable[..., Harness]) -> dict[str, Any]:
    def only_alpha(d: dict[str, Any]) -> None:
        d["routes"] = [{"strategy_id": STRATEGY, "accounts": [A]}]
    h = mk(only_alpha)
    h.start()
    h.publish(build_open(h.now(), "t-c1"))
    h.cycle()
    check(h.ex("t-c1", A)["planned_qty"] == 7 and not h.has_ex("t-c1", B), "original routing alpha only")

    def widen(d: dict[str, Any]) -> None:
        d["routes"] = [{"strategy_id": STRATEGY, "accounts": [A, B]}]
        d["accounts"][0]["initial_margin_per_trade"] = "50.00"
    h.reconfigure(widen)
    check(h.ex("t-c1", A)["planned_qty"] == 7, "existing trade not resized")
    check(h.ex("t-c1", A)["policy_snapshot"]["initial_margin_per_trade"] == "100.00", "snapshot kept")
    h.publish(build_close(h.now(), "t-c1", 2))
    h.cycle()
    check(h.ex("t-c1", A)["state"] == "CLOSED" and not h.has_ex("t-c1", B), "close not broadcast to new accounts")
    h.publish(build_open(h.now(), "t-c2"))
    h.cycle()
    check(h.ex("t-c2", A)["planned_qty"] == 3, "future entry uses new margin (50 x 5)")
    check(h.ex("t-c2", B)["state"] == "OPEN_PROTECTED", "future entry uses new routing")
    try:
        h.reconfigure(lambda d: d["accounts"][0].__setitem__("strategy_owner", "lab_other"))
        raise ScenarioFailure("owner reassignment with live exposure must be rejected")
    except ConfigError as e:
        check("OWNER_REASSIGNMENT_BLOCKED" in str(e), str(e))
    return {"routing": "updates follow original executions", "resize": "future entries only"}


def sc_live_and_unsupported(h: Harness) -> dict[str, Any]:
    h.start()
    h.publish(build_open(h.now(), "t-live", environment="LIVE"))
    h.publish(build_open(h.now(), "t-inverse", instrument={"base": "BTC", "quote": "USD", "settle": "BTC",
                                                           "product_type": "INVERSE_PERPETUAL"}))
    h.publish(build_open(h.now(), "t-usd", source_quote="USD"))
    h.publish(build_open(h.now(), "t-untrusted", producer="someone-else"))
    h.cycle()
    check(h.event("t-live-r1-open")["process_code"] == "LIVE_NOT_SUPPORTED", "live refused")
    check(h.event("t-usd-r1-open")["process_code"] == "CURRENCY_MAPPING_UNSUPPORTED", "USD != USDT")
    check(h.event("t-untrusted-r1-open")["process_code"] == "UNTRUSTED_PRODUCER", "untrusted")
    inv = h.j.q1("SELECT * FROM ingest_log WHERE event_id='t-inverse-r1-open'")
    check(inv is not None and inv["code"] == "UNSUPPORTED_PRODUCT", "inverse perpetual refused")
    check(h.j.q1("SELECT COUNT(*) AS n FROM executions")["n"] == 0, "no executions")  # type: ignore[index]
    check(not h.venue_orders(A) and not h.venue_orders(B), "no orders")
    d = default_config_dict()
    d["mode"] = "live"
    try:
        config_from_dict(d, h.root)
        raise ScenarioFailure("live config must fail")
    except ConfigError as e:
        check("LIVE_NOT_SUPPORTED" in str(e), str(e))
    return {"live": "refused at config and event level"}


def sc_hard_limit_breach_short(mk: Callable[..., Harness]) -> dict[str, Any]:
    def mutate(d: dict[str, Any]) -> None:
        d["accounts"][0]["max_initial_margin_hard"] = "92.00"
    h = mk(mutate)
    h.start()
    # favourable higher-price SHORT fill (book jumps +3000 between quote and match)
    h.sim.add_fault("sim_alpha", "place_entry", "SHIFT_BOOK", account=A, param="3000")
    h.sim.add_fault("sim_alpha", "place_close", "REJECT", account=A, param="SIM_BUSY", count=2)
    h.publish(build_open(h.now(), "t-hl", direction="SHORT"))
    h.cycle()
    a = h.ex("t-hl", A)
    check(a["state"] == "CLOSING", f"close retried, not abandoned ({a['state']})")
    stops = h.venue_orders(A, "STOP_MARKET", "OPEN")
    check(len(stops) == 1 and D(stops[0]["qty"]) == 7, "exposure stays stop-protected while closing")
    h.cycle()
    a = h.ex("t-hl", A)
    check(a["planned_margin"] <= D("92"), "planned margin within the hard limit")
    check(a["actual_margin"] > D("92"), f"actual margin {a['actual_margin']} breached via favourable fill")
    check(a["leverage"] == 5, "leverage unchanged")
    check(a["state"] == "CLOSED" and a["close_reason"] == "EMERGENCY_HARD_LIMIT_BREACH", a["close_reason"])
    check(h.j.account(A)["entries_blocked"] == 1, "account blocked")  # type: ignore[index]
    h.assert_flat_and_clean(A)
    return {"actual_margin": str(a["actual_margin"]), "action": "emergency close + block"}


def sc_unowned_exposure(h: Harness) -> dict[str, Any]:
    h.sim.force_position_order("sim_alpha", A, SYM[A], "BUY", "5")
    h.start()
    inc = h.j.open_incidents(A)
    check(any(i["kind"] == "UNOWNED_EXPOSURE" for i in inc), "unowned exposure incident")
    h.publish(build_open(h.now(), "t-un"))
    h.cycle()
    check(h.ex("t-un", A)["reason_code"] == "ACCOUNT_BLOCKED", "affected account blocked")
    check(h.ex("t-un", B)["state"] == "OPEN_PROTECTED", "other account unaffected")
    check(h.pos(A)["qty"] == 5, "unowned exposure not silently adopted or closed")
    return {"unowned": "incident + block, not adopted"}


def sc_cancel_entry_race(h: Harness) -> dict[str, Any]:
    h.start()
    h.sim.add_fault("sim_alpha", "place_entry", "TIMEOUT_AFTER_ACCEPT", account=A)
    h.sim.add_fault("sim_alpha", "get_order", "STALE_NOT_FOUND", account=A)
    h.publish(build_open(h.now(), "t-cr"))
    h.cycle()
    check(h.ex("t-cr", A)["state"] == "ENTRY_UNKNOWN", "unknown")
    h.publish(build_cancel(h.now(), "t-cr", 2))
    h.cycle()
    a = h.ex("t-cr", A)
    check(a["state"] == "OPEN_PROTECTED" and a["filled_qty"] == 7, "discovered fill is protected")
    check(h.ex("t-cr", B)["state"] == "OPEN_PROTECTED", "beta keeps its position")
    return {"race": "fill discovered after cancel -> managed"}


def sc_amend_exits(h: Harness) -> dict[str, Any]:
    h.start()
    h.publish(build_open(h.now(), "t-am"))
    h.cycle()
    h.market("64800")
    h.publish(build_amend(h.now(), "t-am", 2, stop="64600.10", take_profits=[("66500.10", "1")]))
    h.cycle()
    a = h.ex("t-am", A)
    check(a["exit_plan"]["version"] == 2 and D(a["exit_plan"]["stop"]) == D("64600.10"), "amended")
    check(a["risk_at_stop"] == 0 and a["pnl_at_stop"] > 0, "profit-protecting stop is not negative risk")
    check(a["filled_qty"] == 7 and a["avg_fill"] == D("64500.1") and a["leverage"] == 5,
          "amend never changes qty/entry/leverage")
    for acct in (A, B):
        stops = h.venue_orders(acct, "STOP_MARKET", "OPEN")
        check(len(stops) == 1 and D(stops[0]["trigger_price"]) == D("64600.10"), f"{acct} single new stop")
    h.publish(build_amend(h.now(), "t-am", 3, stop="64900.10", take_profits=[("66500.10", "1")]))
    h.cycle()
    check(h.ex("t-am", A)["exit_plan"]["version"] == 2, "invalid amendment (stop above ref) rejected")
    return {"amend": "stop moved to 64600.10 (profit-protecting), risk 0"}


def sc_worker_lock(h: Harness) -> dict[str, Any]:
    lock1 = ProcessLock(h.cfg.lock_path).acquire()
    try:
        try:
            ProcessLock(h.cfg.lock_path).acquire()
            raise ScenarioFailure("second worker must not acquire the lock")
        except LockHeld:
            pass
    finally:
        lock1.release()
    ProcessLock(h.cfg.lock_path).acquire().release()
    return {"lock": "second instance refused"}


def sc_amend_retry_after_adapter_error(h: Harness) -> dict[str, Any]:
    """A transient adapter error defers (not drops) an amendment; retry is per-execution idempotent."""
    h.start()
    h.publish(build_open(h.now(), "t-ar"))
    h.cycle()
    h.sim.add_fault("sim_alpha", "quote", "TIMEOUT_BEFORE_ACCEPT")  # alpha reference read fails once
    h.publish(build_amend(h.now(), "t-ar", 2, stop="64100.10", take_profits=[("66000.10", "1")]))
    h.cycle()
    check(h.event("t-ar-r2-amend_exits")["process_status"] == "PENDING", "amendment kept pending")
    check(h.ex("t-ar", B)["exit_plan"]["version"] == 2, "healthy account amended immediately")
    check(h.ex("t-ar", A)["exit_plan"]["version"] == 1, "failed account not yet amended")
    h.cycle()
    check(h.event("t-ar-r2-amend_exits")["process_status"] == "APPLIED", "applied on retry")
    for acct in (A, B):
        e = h.ex("t-ar", acct)
        check(e["exit_plan"]["version"] == 2 and D(e["exit_plan"]["stop"]) == D("64100.10"), f"{acct} amended")
        stops = h.venue_orders(acct, "STOP_MARKET", "OPEN")
        check(len(stops) == 1 and D(stops[0]["trigger_price"]) == D("64100.10"), f"{acct} single stop")
    check(len(h.venue_orders(B, "STOP_MARKET")) == 2, "beta not re-amended on retry (original + one)")
    return {"amend": "deferred then applied once per account"}


def sc_tp_failure_degraded(h: Harness) -> dict[str, Any]:
    """A take-profit placement failure with a verified stop is retried, not an emergency close."""
    h.start()
    h.sim.add_fault("sim_beta", "place_tp", "REJECT", account=B, param="SIM_TP_REJECT")
    h.publish(build_open(h.now(), "t-tpf"))
    h.cycle()
    b = h.ex("t-tpf", B)
    check(b["state"] == "OPEN_PROTECTED", f"stop verified -> still protected ({b['state']})")
    check(h.pos(B)["qty"] == 75, "position kept")
    check(any(a["code"] == "TP_PLACEMENT_FAILED" for a in h.alerts), "alerted")
    check(not h.j.account(B)["entries_blocked"], "no block for a TP-only failure")  # type: ignore[index]
    h.cycle()
    check(len(h.venue_orders(B, "TAKE_PROFIT_MARKET", "OPEN")) == 1, "TP placed on retry")
    return {"tp_failure": "degraded + retried; stop kept"}


def sc_stale_stop_observation(h: Harness) -> dict[str, Any]:
    """A single missing observation of a working stop does not create a duplicate stop."""
    h.start()
    h.publish(build_open(h.now(), "t-stale"))
    h.cycle()
    h.sim.add_fault("sim_alpha", "get_order", "STALE_NOT_FOUND", account=A)
    h.cycle()
    h.cycle()
    stops = h.venue_orders(A, "STOP_MARKET")
    check(len(stops) == 1 and stops[0]["status"] == "OPEN", f"no replacement stop placed: {len(stops)}")
    check(h.ex("t-stale", A)["state"] == "OPEN_PROTECTED", "protected again after re-observation")
    h.market("63900", accounts=(A,))
    h.cycle()
    a = h.ex("t-stale", A)
    check(a["state"] == "CLOSED" and a["close_reason"] == "STOP_LOSS", a["close_reason"])
    check(a["net_pnl"] == a["realized_pnl"] - a["exit_fees"] - a["entry_fees"] and a["exit_fees"] > 0,
          "complete accounting")
    return {"stale": "no duplicate stop"}


@dataclass(frozen=True)
class Scenario:
    name: str
    fn: Callable[..., dict[str, Any]]
    custom_harness: bool = False


SCENARIOS: dict[str, Scenario] = {s.name: s for s in [
    Scenario("long_reference_two_venues", sc_long_reference_two_venues),
    Scenario("short_reference_stop", sc_short_reference_stop),
    Scenario("price_eligibility", sc_price_eligibility),
    Scenario("preflight_rejections", sc_preflight_rejections, True),
    Scenario("entry_relative", sc_entry_relative, True),
    Scenario("source_risk_informational", sc_source_risk_informational),
    Scenario("ioc_outcomes", sc_ioc_outcomes),
    Scenario("partial_infeasible_emergency", sc_partial_infeasible_emergency),
    Scenario("entry_timeout_after_accept_recovery", sc_entry_timeout_after_accept_recovery),
    Scenario("entry_timeout_before_accept", sc_entry_timeout_before_accept),
    Scenario("protection_failure", sc_protection_failure),
    Scenario("close_paths", sc_close_paths),
    Scenario("downtime_stop_and_deadline", sc_downtime_stop_and_deadline),
    Scenario("event_ordering", sc_event_ordering),
    Scenario("crash_around_ingestion", sc_crash_around_ingestion),
    Scenario("multi_tp_and_competing_exits", sc_multi_tp_and_competing_exits),
    Scenario("pause_and_disable", sc_pause_and_disable),
    Scenario("config_change_routing", sc_config_change_routing, True),
    Scenario("live_and_unsupported", sc_live_and_unsupported),
    Scenario("hard_limit_breach_short", sc_hard_limit_breach_short, True),
    Scenario("unowned_exposure", sc_unowned_exposure),
    Scenario("cancel_entry_race", sc_cancel_entry_race),
    Scenario("amend_exits", sc_amend_exits),
    Scenario("worker_lock", sc_worker_lock),
    Scenario("amend_retry_after_adapter_error", sc_amend_retry_after_adapter_error),
    Scenario("tp_failure_degraded", sc_tp_failure_degraded),
    Scenario("stale_stop_observation", sc_stale_stop_observation),
]}


def run_scenario(name: str, root: str | Path | None = None) -> dict[str, Any]:
    sc = SCENARIOS[name]
    if root is None:
        with tempfile.TemporaryDirectory(prefix=f"aseke_{name}_") as tmp:
            return run_scenario(name, tmp)
    root = Path(root)
    made: list[Harness] = []

    def mk(mutate: Callable[[dict[str, Any]], None] | None = None) -> Harness:
        h = Harness(root / f"h{len(made)}", mutate)
        made.append(h)
        return h

    try:
        return sc.fn(mk) if sc.custom_harness else sc.fn(mk())
    finally:
        for h in made:
            if h.journal is not None:
                h.journal.close()
            h.sim.close()
