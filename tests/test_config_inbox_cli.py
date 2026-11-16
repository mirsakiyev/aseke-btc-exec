"""Config validation, atomic publisher/scanner, and the documented CLI demonstration."""

import json
import shutil
from pathlib import Path

import pytest

from aseke_exec.cli import main
from aseke_exec.config import ConfigError, config_from_dict, load_config
from aseke_exec.inbox import Inbox, SignalExists, publish_event
from aseke_exec.scenarios import default_config_dict

ROOT = Path(__file__).resolve().parents[1]


def test_sample_config_is_valid_and_matches_scenario_defaults():
    cfg = load_config(ROOT / "config/sim.toml")
    ref = config_from_dict(default_config_dict(), ROOT / "config")
    assert cfg.mode == "simulation"
    assert {a: c.snapshot() for a, c in cfg.accounts.items()} == \
        {a: c.snapshot() for a, c in ref.accounts.items()}
    assert cfg.routes == ref.routes
    assert {v: sorted(c.instruments) for v, c in cfg.sim.venues.items()} == \
        {v: sorted(c.instruments) for v, c in ref.sim.venues.items()}


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.__setitem__("mode", "live"), "LIVE_NOT_SUPPORTED"),
    (lambda d: d["accounts"][0].__setitem__("initial_margin_per_trade", 100.0), "floats not accepted"),
    (lambda d: d["accounts"][0].__setitem__("source_risk_enforcement", "ACCOUNT_EQUITY"), "not implemented"),
    (lambda d: d["accounts"][0].__setitem__("margin_mode", "CROSS"), "cross margin is never used"),
    (lambda d: d["accounts"][0].__setitem__("id", "mexc_main"), "clearly synthetic"),
    (lambda d: d["accounts"][0].__setitem__("strategy_owner", ""), "strategy_owner is required"),
])
def test_invalid_config_fails_clearly(mutate, needle, tmp_path):
    d = default_config_dict()
    mutate(d)
    with pytest.raises(ConfigError) as ei:
        config_from_dict(d, tmp_path)
    assert needle in str(ei.value)


def test_publisher_is_atomic_and_never_overwrites(tmp_path):
    ev = {"event_id": "e-1", "created_at": "2026-01-05T12:00:00.000Z"}
    p = publish_event(tmp_path / "inbox", ev)
    assert json.loads(p.read_text())["event_id"] == "e-1"
    with pytest.raises(SignalExists):
        publish_event(tmp_path / "inbox", ev)
    (tmp_path / "inbox" / ".half.json.tmp").write_text("{", encoding="utf-8")
    (tmp_path / "inbox" / "notes.txt").write_text("x", encoding="utf-8")
    assert [x.name for x in Inbox(tmp_path / "inbox").pending()] == [p.name]
    assert not list((tmp_path / "inbox").glob(".*.tmp")) or \
        [x.name for x in (tmp_path / "inbox").glob(".*.tmp")] == [".half.json.tmp"]


def test_documented_cli_demo_end_to_end(tmp_path, capsys):
    """Mirrors the README demonstration (system clock, no network, no keys)."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    shutil.copy(ROOT / "config/sim.toml", cfg_dir / "sim.toml")
    c = ["--config", str(cfg_dir / "sim.toml")]
    assert main(c + ["validate-config"]) == 0
    assert main(c + ["sim", "init", "--reset"]) == 0
    assert main(c + ["publish-example", "long", "--trade-id", "demo-long-1"]) == 0
    assert main(c + ["cycle"]) == 0
    capsys.readouterr()
    assert main(c + ["status", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    states = {e["account_id"]: e["state"] for e in rep["recent_executions"]}
    assert states == {"sim_alpha_a1": "OPEN_PROTECTED", "sim_beta_b1": "OPEN_PROTECTED"}
    assert main(c + ["pause"]) == 0
    assert main(c + ["sim", "set-market", "--price", "66100"]) == 0
    assert main(c + ["cycle"]) == 0
    capsys.readouterr()
    assert main(c + ["status", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert {e["close_reason"] for e in rep["recent_executions"]} == {"TAKE_PROFIT"}
    assert rep["controls"]["pause_new_entries"] == "1"
    assert (tmp_path / "var" / "status.json").exists()
    assert (tmp_path / "var" / "archive").exists()
