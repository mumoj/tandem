"""Tell a thinking implementer from a stuck one.

OpenCode reports one status, ``busy``, for a turn in very different states. The
mix of events arriving is the only thing that separates them:

=================  =========================================================
working            tool calls arriving, or a tool still running
reasoning_stall    text keeps arriving but no tool has run for a long time
blocked            a permission or question is waiting on a human
hung               nothing has arrived for a long time
error              the turn ended on a provider or model error
idle               no turn running
=================  =========================================================

The reasoning stall is the one nothing else catches. On one task the
implementer produced about 9,000 characters of reasoning over 25 minutes and
wrote nothing: the spec contradicted itself and it would not guess past the
contradiction. Restarting would have thrown the reasoning away and hit the same
wall. The useful response was to fix the spec, which needs someone to notice.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel

from tandem.opencode.events import ABORTED, Event
from tandem.opencode.jsonpath import dig, dig_str


class Activity(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    REASONING_STALL = "reasoning_stall"
    BLOCKED = "blocked"
    HUNG = "hung"
    ERROR = "error"


_RUNNING_TOOL_STATES = frozenset({"pending", "running"})


class ActivityReport(BaseModel):
    """The numbers behind a classification, for a human deciding what to do."""

    activity: Activity
    busy: bool
    seconds_since_tool: int
    seconds_since_text: int
    running_tools: list[str]
    pending: list[str]
    error: str | None
    retry: str | None


@dataclass
class ActivityTracker:
    """Fold one session's events into an ``Activity``.

    Feed it every event with ``observe``; it ignores events for other sessions.
    ``classify`` is cheap and side-effect free, so callers can ask as often as
    they like.
    """

    stall_after: float = 720.0
    quiet_after: float = 300.0
    text_recent: float = 120.0
    clock: Callable[[], float] = time.monotonic

    session_id: str | None = None
    busy: bool = False
    error: str | None = None
    retry: str | None = None
    last_tool: float = 0.0
    last_text: float = 0.0
    turn_started_at: float = 0.0
    pending: set[str] = field(default_factory=set[str])
    running_tools: dict[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        self.last_tool = self.last_text = self.turn_started_at = self.clock()

    def track(self, session_id: str | None) -> None:
        """Start following a different session, forgetting the previous one."""
        self.session_id = session_id
        self.busy = False
        self.error = None
        self.retry = None
        self.pending.clear()
        self.running_tools.clear()
        self.last_tool = self.last_text = self.clock()

    def turn_started(self) -> None:
        """A prompt was sent. Busy from now, before the server says so.

        Without this, a ``wait`` issued straight after a prompt would see the
        session as idle and report the turn finished before it began.
        """
        now = self.clock()
        self.busy = True
        self.error = None
        self.retry = None
        self.last_tool = self.last_text = self.turn_started_at = now

    def sync_pending(self, request_ids: set[str]) -> None:
        """Replace the pending set with what the server reports.

        Used after the event stream reconnects, when replies may have been
        missed.
        """
        self.pending = set(request_ids)

    def sync_busy(self, busy: bool, *, grace: float = 10.0) -> None:
        """Adopt the server's view of whether the turn is running.

        A prompt sent moments ago may not have reached the server's status yet,
        so a fresh turn is not ended on the strength of a stale ``idle``.
        """
        if busy and not self.busy:
            self.turn_started()
        elif not busy and self.busy and self.clock() - self.turn_started_at > grace:
            self.turn_ended()

    def observe(self, event: Event) -> None:
        if self.session_id is None or event.session_id != self.session_id:
            return
        now = self.clock()
        match event.type:
            case "session.status":
                kind = event.prop("status", "type")
                if kind in ("busy", "retry"):
                    if not self.busy:
                        self.turn_started()
                    self.retry = _retry_detail(event) if kind == "retry" else None
                elif kind == "idle":
                    self.turn_ended()
            case "session.idle":
                self.turn_ended()
            case "message.part.updated":
                self._observe_part(event.prop("part"), now)
            case "message.part.delta":
                if event.prop("field") == "text":
                    self.last_text = now
            case "permission.asked" | "question.asked":
                if event.request_id:
                    self.pending.add(event.request_id)
            case "permission.replied" | "question.replied" | "question.rejected":
                if event.request_id:
                    self.pending.discard(event.request_id)
            case "session.error":
                if event.error_name != ABORTED:
                    name = event.error_name or "error"
                    self.error = f"{name}: {event.error_message}"[:300]
                self.turn_ended()
            case _:
                pass

    def _observe_part(self, part: object, now: float) -> None:
        kind = dig_str(part, "type")
        if kind == "tool":
            self.last_tool = now
            call = dig_str(part, "callID") or dig_str(part, "id") or ""
            if dig(part, "state", "status") in _RUNNING_TOOL_STATES:
                self.running_tools[call] = dig_str(part, "tool") or "tool"
            else:
                self.running_tools.pop(call, None)
        elif kind in ("text", "reasoning"):
            self.last_text = now

    def turn_ended(self) -> None:
        """The turn finished, failed, or never started."""
        self.busy = False
        self.retry = None
        self.running_tools.clear()

    def classify(self) -> Activity:
        if self.pending:
            return Activity.BLOCKED
        if self.error and not self.busy:
            return Activity.ERROR
        if not self.busy:
            return Activity.IDLE
        now = self.clock()
        tool_idle = now - self.last_tool
        text_idle = now - self.last_text
        if self.running_tools:
            # A long test run is work, not a hang. A tool that has produced no
            # update for as long as a reasoning stall takes is probably stuck
            # on input it will never get.
            return Activity.WORKING if tool_idle < self.stall_after else Activity.HUNG
        if text_idle < self.text_recent and tool_idle > self.stall_after:
            return Activity.REASONING_STALL
        if text_idle > self.quiet_after and tool_idle > self.quiet_after:
            return Activity.HUNG
        return Activity.WORKING

    def describe(self) -> ActivityReport:
        now = self.clock()
        return ActivityReport(
            activity=self.classify(),
            busy=self.busy,
            seconds_since_tool=round(now - self.last_tool),
            seconds_since_text=round(now - self.last_text),
            running_tools=sorted(set(self.running_tools.values())),
            pending=sorted(self.pending),
            error=self.error,
            retry=self.retry,
        )


def _retry_detail(event: Event) -> str:
    message = dig_str(event.properties, "status", "message") or ""
    return f"attempt {event.prop('status', 'attempt')}: {message[:200]}"
