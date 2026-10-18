from __future__ import annotations

import pytest

from tandem.supervisor.activity import Activity, ActivityTracker
from tests.fakes import FakeClock, event

SESSION = "ses_1"


@pytest.fixture
def tracker(clock: FakeClock) -> ActivityTracker:
    tracker = ActivityTracker(clock=clock, stall_after=720, quiet_after=300, text_recent=120)
    tracker.track(SESSION)
    tracker.turn_started()
    return tracker


def text_delta() -> object:
    return event(
        "message.part.delta",
        sessionID=SESSION,
        messageID="msg_1",
        partID="prt_1",
        field="text",
        delta="thinking",
    )


def tool_part(status: str, call: str = "call_1") -> object:
    part = {
        "id": "prt_2",
        "sessionID": SESSION,
        "messageID": "msg_1",
        "type": "tool",
        "callID": call,
        "tool": "bash",
        "state": {"status": status},
    }
    return event("message.part.updated", sessionID=SESSION, part=part, time=0)


def test_a_fresh_turn_is_working(tracker: ActivityTracker) -> None:
    assert tracker.classify() is Activity.WORKING


def test_text_without_tools_becomes_a_reasoning_stall(
    tracker: ActivityTracker, clock: FakeClock
) -> None:
    for _ in range(7):
        clock.advance(110)
        tracker.observe(text_delta())  # type: ignore[arg-type]
    assert clock.now - tracker.last_tool > 720
    assert tracker.classify() is Activity.REASONING_STALL


def test_silence_becomes_hung(tracker: ActivityTracker, clock: FakeClock) -> None:
    clock.advance(301)
    assert tracker.classify() is Activity.HUNG


def test_a_long_running_tool_is_work_until_it_goes_quiet(
    tracker: ActivityTracker, clock: FakeClock
) -> None:
    tracker.observe(tool_part("running"))  # type: ignore[arg-type]
    clock.advance(600)  # a ten-minute test run
    assert tracker.classify() is Activity.WORKING
    clock.advance(200)
    assert tracker.classify() is Activity.HUNG

    tracker.observe(tool_part("completed"))  # type: ignore[arg-type]
    assert tracker.running_tools == {}


def test_a_pending_request_blocks_until_answered(tracker: ActivityTracker) -> None:
    tracker.observe(event("permission.asked", id="per_1", sessionID=SESSION))
    assert tracker.classify() is Activity.BLOCKED
    tracker.observe(event("permission.replied", sessionID=SESSION, requestID="per_1", reply="once"))
    assert tracker.classify() is Activity.WORKING


def test_questions_block_too(tracker: ActivityTracker) -> None:
    tracker.observe(event("question.asked", id="que_1", sessionID=SESSION, questions=[]))
    assert tracker.classify() is Activity.BLOCKED
    tracker.observe(event("question.rejected", sessionID=SESSION, requestID="que_1"))
    assert tracker.classify() is Activity.WORKING


def test_idle_and_errors(tracker: ActivityTracker) -> None:
    tracker.observe(event("session.status", sessionID=SESSION, status={"type": "idle"}))
    assert tracker.classify() is Activity.IDLE

    tracker.observe(event("session.status", sessionID=SESSION, status={"type": "busy"}))
    error = {"name": "APIError", "data": {"message": "overloaded"}}
    tracker.observe(event("session.error", sessionID=SESSION, error=error))
    assert tracker.classify() is Activity.ERROR
    assert tracker.error == "APIError: overloaded"


def test_an_abort_is_not_an_error(tracker: ActivityTracker) -> None:
    error = {"name": "MessageAbortedError", "data": {"message": "aborted"}}
    tracker.observe(event("session.error", sessionID=SESSION, error=error))
    assert tracker.classify() is Activity.IDLE


def test_retry_is_reported(tracker: ActivityTracker) -> None:
    status = {"type": "retry", "attempt": 2, "message": "429 slow down", "next": 0}
    tracker.observe(event("session.status", sessionID=SESSION, status=status))
    assert tracker.retry == "attempt 2: 429 slow down"
    assert tracker.describe().retry == tracker.retry


def test_other_sessions_are_ignored(tracker: ActivityTracker) -> None:
    tracker.observe(event("session.idle", sessionID="ses_other"))
    assert tracker.classify() is Activity.WORKING


def test_a_stale_idle_does_not_end_a_fresh_turn(tracker: ActivityTracker, clock: FakeClock) -> None:
    tracker.sync_busy(False)
    assert tracker.busy
    clock.advance(11)
    tracker.sync_busy(False)
    assert not tracker.busy
