"""Reach a human who is not watching the terminal.

A headless session that is waiting on a person stalls indefinitely, and
nothing else times it out. Polling is not a plan, so the events that stop
forward progress are pushed.

Only those are escalated. A finished turn is for the planner's ``wait``, not
for a person's attention, and a notification nobody needed is worse than none:
it trains the reader to ignore the channel that carries the real failure. The
same reasoning drops ``MessageAbortedError``, which the supervisor raises itself
every time it replaces a session.

Everything is still recorded in ``notifications.log``. Delivery never blocks
the caller: the supervisor consumes the event stream inline, so a slow
notifier would delay the next report.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from tandem.opencode.events import ABORTED, Event
from tandem.opencode.jsonpath import dig_list, dig_str

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Alert:
    kind: str
    summary: str
    session_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict[str, Any])

    @classmethod
    def from_event(cls, event: Event) -> Alert | None:
        """The alert an event deserves, or ``None`` if nobody needs to know."""
        match event.type:
            case "permission.asked":
                patterns = ", ".join(str(p) for p in dig_list(event.properties, "patterns"))
                summary = (
                    f"{event.request_id} wants {event.prop('permission')} on {patterns[:120]}; "
                    "answer with `tandem allow` or `tandem deny`"
                )
            case "question.asked":
                first = next(iter(dig_list(event.properties, "questions")), None)
                asked = dig_str(first, "question") or dig_str(first, "header") or ""
                summary = f"{event.request_id} asks: {asked[:160]}; answer with `tandem answer`"
            case "session.error":
                if event.error_name == ABORTED:
                    return None
                summary = f"{event.error_name or 'error'}: {event.error_message[:300]}"
            case _:
                return None
        return cls(event.type, summary, event.session_id, event.model_dump(mode="json"))


class Notifier:
    def __init__(
        self,
        *,
        log_path: Path,
        task: str,
        state_dir: Path,
        command: str | None = None,
        ntfy_topic: str | None = None,
        ntfy_server: str = "https://ntfy.sh",
        timeout: float = 30.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.log_path = log_path
        self.task = task
        self.state_dir = state_dir
        self.command = command
        self.ntfy_topic = ntfy_topic
        self.ntfy_url = f"{ntfy_server.rstrip('/')}/{ntfy_topic}" if ntfy_topic else None
        self.timeout = timeout
        self._http = http
        # Strong references: asyncio keeps only weak ones, and a delivery task
        # collected mid-flight would vanish without a trace.
        self._tasks: set[asyncio.Task[None]] = set()

    def escalate(self, alert: Alert) -> None:
        self._record(alert)
        if self.command:
            self._spawn(self._run_command(alert))
        if self.ntfy_url:
            self._spawn(self._publish(alert))

    def _record(self, alert: Alert) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = f"{stamp}\t{self.task}\t{alert.kind}\t{alert.summary}\n"
        descriptor = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, line.encode())
        finally:
            os.close(descriptor)
        log.warning("alert %s %s", alert.kind, alert.summary)

    def _spawn(self, coroutine: Any) -> None:
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_command(self, alert: Alert) -> None:
        assert self.command is not None
        env = {
            **os.environ,
            "TANDEM_NOTIFY": alert.summary,
            "TANDEM_KIND": alert.kind,
            "TANDEM_EVENT": json.dumps(alert.payload),
            "TANDEM_TASK": self.task,
            "TANDEM_STATE_DIR": str(self.state_dir),
        }
        try:
            process = await asyncio.create_subprocess_shell(
                self.command,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            log.error("notify command failed to start: %s", exc)
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=self.timeout)
        except TimeoutError:
            # A hung notifier must not pile up: kill it and carry on.
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            log.error("notify command timed out after %ss", self.timeout)
        else:
            if process.returncode:
                log.error("notify command exited %s", process.returncode)

    async def _publish(self, alert: Alert) -> None:
        assert self.ntfy_url is not None
        http = self._http or httpx.AsyncClient(timeout=self.timeout)
        try:
            response = await http.post(
                self.ntfy_url,
                content=alert.summary.encode(),
                headers={"Title": f"tandem {self.task}: {alert.kind}"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("ntfy publish failed: %s", exc)
        finally:
            if self._http is None:
                await http.aclose()

    async def drain(self) -> None:
        """Wait for deliveries in flight. Called once, at shutdown."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=self.timeout)
