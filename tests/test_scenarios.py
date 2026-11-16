"""Acceptance scenarios: full lifecycle against the persistent deterministic simulator.

Each scenario (src/aseke_exec/scenarios.py) asserts invariants about venue orders,
exposure, protection coverage, persisted journal state and accounting.
"""

import logging

import pytest

from aseke_exec.scenarios import SCENARIOS, run_scenario


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario(name, tmp_path):
    result = run_scenario(name, tmp_path)
    assert isinstance(result, dict)
