"""Local JSON-file transport: atomic publisher, scanner, archive and quarantine.

Publisher contract: write the complete JSON to a hidden temp file in the SAME
directory (``.<name>.tmp``), flush + fsync + close, then publish with a
same-volume rename that refuses to overwrite an existing signal. The scanner
consumes only ``*.json`` files whose names do not start with '.'.

File movement is NOT the ledger: the engine journals an event durably before
the file is archived, and duplicates are detected by event identity + hash.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .contract import canonical_json

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class SignalExists(Exception):
    pass


def signal_filename(event: dict[str, Any]) -> str:
    created = str(event.get("created_at", "")).replace(":", "").replace("-", "").replace(".", "")
    return f"{_SAFE.sub('_', created)}__{_SAFE.sub('_', str(event.get('event_id', 'unknown')))}.json"


def publish_event(inbox_dir: str | Path, event: dict[str, Any], filename: str | None = None) -> Path:
    inbox = Path(inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    name = filename or signal_filename(event)
    final = inbox / name
    if final.exists():
        raise SignalExists(f"signal file already exists: {final}")
    tmp = inbox / f".{name}.{os.getpid()}.tmp"
    data = json.dumps(event, indent=2, sort_keys=True, ensure_ascii=False,
                      default=lambda o: format(o, "f")).encode("utf-8")
    with open(tmp, "xb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        if os.name == "nt":
            os.rename(tmp, final)  # raises FileExistsError instead of overwriting
        else:
            os.link(tmp, final)  # atomic no-clobber publish on POSIX
            os.unlink(tmp)
    except FileExistsError:
        os.unlink(tmp)
        raise SignalExists(f"signal file already exists: {final}") from None
    return final


class Inbox:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.archive_dir = self.root.parent / "archive"
        self.quarantine_dir = self.root.parent / "quarantine"
        for d in (self.root, self.archive_dir, self.quarantine_dir):
            d.mkdir(parents=True, exist_ok=True)

    def pending(self) -> list[Path]:
        return sorted(p for p in self.root.iterdir()
                      if p.is_file() and p.suffix == ".json" and not p.name.startswith("."))

    @staticmethod
    def _move(src: Path, dest_dir: Path) -> Path:
        dest = dest_dir / src.name
        n = 1
        while dest.exists():
            dest = dest_dir / f"{src.stem}.dup{n}{src.suffix}"
            n += 1
        os.replace(src, dest)
        return dest

    def archive(self, path: Path) -> Path:
        return self._move(path, self.archive_dir)

    def quarantine(self, path: Path, code: str, message: str) -> Path:
        dest = self._move(path, self.quarantine_dir)
        reason = dest.with_suffix(dest.suffix + ".reason.json")
        reason.write_text(json.dumps({"code": code, "message": message}, indent=2), encoding="utf-8")
        return dest


__all__ = ["Inbox", "publish_event", "signal_filename", "SignalExists", "canonical_json"]
