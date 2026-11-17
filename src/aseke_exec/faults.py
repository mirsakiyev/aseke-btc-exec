"""Test-only process crash points.

When the environment variable ``ASEKE_EXEC_CRASHPOINT`` names one of the boundaries passed
to :func:`crashpoint`, the process terminates immediately with ``os._exit(97)`` - no
``finally`` blocks, no journal close, no flush - to model an abrupt process crash at a
controlled boundary. Used only by tests/test_process_crash.py against isolated runtime roots.
This models a *process* crash; it is not evidence of host power-loss safety.

Boundary names (``<leg>`` is ENTRY / STOP / TP / CLOSE, ``v<n>`` the exit-plan version):
  after_ingest_commit          event journaled, file not yet archived
  after_intent:<leg>[:v<n>]    order intent + client id persisted, not yet submitted
  after_ack:<leg>[:v<n>]       venue accepted/filled the order, local ack not yet recorded
  before_close_accounting      venue flat + final sync done, CLOSED not yet committed
"""

from __future__ import annotations

import os

ENV = "ASEKE_EXEC_CRASHPOINT"
EXIT_CODE = 97


def crashpoint(*names: str) -> None:
    spec = os.environ.get(ENV)
    if spec and spec in names:
        os._exit(EXIT_CODE)
