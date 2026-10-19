"""OpenCode's event stream: the envelope, and a recorder for the raw lines.

The stream is the only place some facts exist. A permission request blocks the
model but appears in neither the session status nor its messages, and a turn
that dies on a provider error looks exactly like ongoing work everywhere else.
So the supervisor consumes every event, and the raw stream is kept on disk as
evidence.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Self, cast

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from tandem.opencode.jsonpath import dig, dig_str

ABORTED = "MessageAbortedError"
"""The error OpenCode raises for an abort. Routine when a spec is replaced."""


class Event(BaseModel):
    """One bus event. ``properties`` stays a plain dict on purpose.

    OpenCode publishes nearly a hundred event types and adds more in minor
    releases. Modelling each would make the supervisor brittle against the one
    it does not care about; the handful it acts on are read by key.
    """

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    type: str
    properties: dict[str, Any] = Field(default_factory=dict[str, Any])
    _raw: str = PrivateAttr(default="")

    @classmethod
    def parse(cls, data: str) -> Event | None:
        """Parse one SSE ``data`` payload, or ``None`` if it is not an event."""
        try:
            event = cls.model_validate_json(data)
        except ValidationError:
            return None
        event._raw = data
        return event

    @classmethod
    def parse_global(cls, data: str, directory: str) -> Event | None:
        """One ``/global/event`` payload, if it belongs to ``directory``.

        A server shared by several tasks wraps every event as
        ``{"directory": ..., "payload": event}``. Events without a directory,
        such as ``server.connected``, concern every listener and are kept.
        """
        try:
            envelope = json.loads(data)
        except ValueError:
            return None
        if not isinstance(envelope, dict):
            return None
        wrapped = cast(dict[str, Any], envelope)
        if wrapped.get("directory") not in (None, directory):
            return None
        try:
            event = cls.model_validate(wrapped.get("payload"))
        except ValidationError:
            return None
        event._raw = data
        return event

    @property
    def raw(self) -> str:
        return self._raw

    def prop(self, *keys: str) -> object:
        """A nested property, or ``None``: ``event.prop("status", "type")``."""
        return dig(self.properties, *keys)

    @property
    def session_id(self) -> str | None:
        """The session an event belongs to, wherever this event type keeps it."""
        return (
            dig_str(self.properties, "sessionID")
            or dig_str(self.properties, "part", "sessionID")
            or dig_str(self.properties, "info", "sessionID")
        )

    @property
    def request_id(self) -> str | None:
        """The permission or question id: ``id`` when asked, ``requestID`` once settled."""
        return dig_str(self.properties, "requestID") or dig_str(self.properties, "id")

    @property
    def error_name(self) -> str | None:
        return dig_str(self.properties, "error", "name")

    @property
    def error_message(self) -> str:
        return dig_str(self.properties, "error", "data", "message") or ""


# ``"type"`` sits near the head of every event, so a bounded window is enough to
# label an oversized line without decoding megabytes of it.
_TYPE_PATTERN = re.compile(r'"type"\s*:\s*"([A-Za-z0-9_.-]+)"')
_TYPE_SCAN = 512


class StreamRecorder:
    """Append the raw stream to disk, with oversized lines replaced by a stub.

    OpenCode re-broadcasts a whole message on every update, so a compaction
    summary can arrive repeatedly at several megabytes. On one task those lines
    were 80% of a 218 MB log and nothing ever read them back, so the recorder
    notes that one arrived and how big it was instead of keeping it.

    Flushing is batched: per-line flushes cost 200k syscalls on one task. A
    caller flushes explicitly on events worth acting on, and the recorder
    flushes on its own at most once a second otherwise.
    """

    def __init__(self, path: Path, max_line: int, *, flush_every: float = 1.0) -> None:
        self.path = path
        self.max_line = max_line
        self.flush_every = flush_every
        self.dropped = 0
        self.dropped_bytes = 0
        self._sink: IO[str] | None = None
        self._last_flush = 0.0

    def __enter__(self) -> Self:
        # 0600: every file the model read and every command's output passes
        # through this stream.
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._sink = os.fdopen(descriptor, "a", encoding="utf-8")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._sink is not None:
            self._sink.close()
            self._sink = None

    def write(self, data: str, *, flush: bool = False) -> None:
        if self._sink is None:
            raise RuntimeError("StreamRecorder used outside its context")
        size = len(data)
        if 0 < self.max_line < size:
            match = _TYPE_PATTERN.search(data[:_TYPE_SCAN])
            stub = {
                "type": match.group(1) if match else "unknown",
                "tandemDropped": True,
                "bytes": size,
            }
            data = json.dumps(stub, separators=(",", ":"))
            self.dropped += 1
            self.dropped_bytes += size
        self._sink.write(f"data: {data}\n")
        now = time.monotonic()
        if flush or now - self._last_flush >= self.flush_every:
            self._sink.flush()
            self._last_flush = now
