"""An append-only record of everything the supervisor did and saw.

This is the file a reviewer greps. It holds one line per state change, plus
the stream events that end forward progress, so a turn that died mid-flight
leaves a trace here without anyone reading a multi-megabyte session capture.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

from tandem.supervisor.state import dump_jsonl

log = logging.getLogger("tandem.journal")


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, action: str, **details: Any) -> None:
        """Record ``action``. ``time`` and ``action`` are written last.

        A caller's detail key can therefore never silently redefine them: this
        is the audit trail for every intervention, so its own schema has to be
        trustworthy.
        """
        record = {**details, "time": round(time.time(), 3), "action": action}
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            # One write() per line on an O_APPEND descriptor: lines never interleave.
            os.write(descriptor, dump_jsonl(record).encode())
        finally:
            os.close(descriptor)
        summary = " ".join(f"{k}={v}" for k, v in details.items() if v not in (None, "", []))
        log.info("%s %s", action, summary)

    def tail(self, count: int) -> list[str]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as stream:
            return [line.rstrip("\n") for line in deque(stream, maxlen=count)]
