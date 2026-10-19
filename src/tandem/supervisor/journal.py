"""An append-only record of everything the supervisor did and saw.

This is the file a reviewer greps. It holds one line per state change, plus
the stream events that end forward progress, so a turn that died mid-flight
leaves a trace here without anyone reading a multi-megabyte session capture.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

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

    def read_from(self, offset: int | None, limit: int) -> tuple[list[dict[str, Any]], int]:
        """Whole lines after byte ``offset``, and the offset to continue from.

        ``None`` starts from the last ``limit`` lines. A line still being
        written is left for the next call rather than returned half-parsed.
        """
        if not self.path.exists():
            return [], 0
        with self.path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            if offset is None or offset > size:
                stream.seek(0)
                lines = deque(stream, maxlen=limit)
                return _parse(lines), size
            stream.seek(offset)
            entries: list[bytes] = []
            position = offset
            for line in stream:
                if not line.endswith(b"\n") or len(entries) >= limit:
                    break
                entries.append(line)
                position += len(line)
            return _parse(entries), position

    def tail(self, count: int) -> list[str]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as stream:
            return [line.rstrip("\n") for line in deque(stream, maxlen=count)]


def _parse(lines: Iterable[bytes]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in lines:
        with contextlib.suppress(ValueError):
            entry = json.loads(line)
            if isinstance(entry, dict):
                entries.append(cast(dict[str, Any], entry))
    return entries
