from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from tandem.config import TaskPaths
from tandem.opencode.models import PermissionAction
from tandem.supervisor.prompts import directive_marker, spec_marker
from tandem.supervisor.state import (
    DirectiveAction,
    SpecRecord,
    SpecStatus,
    StateStore,
    SupervisorState,
)
from tandem.supervisor.supervisor import (
    ConflictError,
    NoActiveSpecError,
    Supervisor,
    WaitReason,
    digest,
)
from tests.conftest import SupervisorFactory
from tests.fakes import FakeClock, FakeOpenCode, event


def journal_actions(paths: TaskPaths) -> list[str]:
    lines = paths.journal.read_text().splitlines()
    return [json.loads(line)["action"] for line in lines]


def write_result(supervisor: Supervisor, verdict: str = "done", spec_id: str = "S-1") -> Path:
    record = supervisor.state.current
    assert record is not None
    path = supervisor.result_path(record)
    path.write_text(json.dumps({"spec_id": spec_id, "verdict": verdict, "summary": "ok"}))
    return path


# ---- specs ---------------------------------------------------------------------


async def test_spec_gets_a_session_with_protected_files_gated(
    supervisor: Supervisor, api: FakeOpenCode, paths: TaskPaths
) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")

    session = api.sessions[record.session_id]
    asks = [r.pattern for r in session.rules if r.action is PermissionAction.ASK]
    assert asks == ["instruction.md"]
    # The broad allow must come first: OpenCode lets the last matching rule win.
    assert session.rules[0].action is PermissionAction.ALLOW
    [prompt] = session.prompts
    assert prompt.startswith(spec_marker("S-1"))
    assert "Add a greeting." in prompt
    assert str(supervisor.result_path(record)) in prompt
    assert record.status is SpecStatus.ACTIVE
    assert journal_actions(paths)[-2:] == ["session_created", "spec_delivered"]


async def test_resubmitting_the_same_spec_is_a_no_op(
    supervisor: Supervisor, api: FakeOpenCode
) -> None:
    first = await supervisor.submit_spec("S-1", "Add a greeting.")
    again = await supervisor.submit_spec("S-1", "  Add a greeting.\n")

    assert again.session_id == first.session_id
    assert len(api.sessions) == 1
    assert len(api.prompts(first.session_id)) == 1


async def test_a_spec_id_cannot_change_its_text(supervisor: Supervisor) -> None:
    await supervisor.submit_spec("S-1", "Add a greeting.")
    with pytest.raises(ConflictError, match="immutable"):
        await supervisor.submit_spec("S-1", "Add a farewell.")


async def test_a_new_spec_replaces_the_session(supervisor: Supervisor, api: FakeOpenCode) -> None:
    first = await supervisor.submit_spec("S-1", "Add a greeting.")
    second = await supervisor.submit_spec("S-2", "Add a farewell.")

    assert second.session_id != first.session_id
    assert first.session_id in api.aborted
    assert supervisor.state.specs["S-1"].status is SpecStatus.SUPERSEDED
    with pytest.raises(ConflictError, match="superseded"):
        await supervisor.submit_spec("S-1", "Add a greeting.")


async def test_a_failed_delivery_is_retried_by_resubmitting(
    supervisor: Supervisor, api: FakeOpenCode
) -> None:
    api.fail_prompts = True
    with pytest.raises(Exception, match="boom"):
        await supervisor.submit_spec("S-1", "Add a greeting.")
    record = supervisor.state.specs["S-1"]
    assert record.status is SpecStatus.PROMPT_PENDING
    attention = supervisor.attention()
    assert attention is not None
    assert attention.reason is WaitReason.ERROR

    api.fail_prompts = False
    await supervisor.submit_spec("S-1", "Add a greeting.")
    assert record.status is SpecStatus.ACTIVE
    assert len(api.prompts(record.session_id)) == 1


# ---- directives ----------------------------------------------------------------


async def test_a_directive_cannot_outlive_its_spec(supervisor: Supervisor) -> None:
    await supervisor.submit_spec("S-1", "Add a greeting.")
    await supervisor.submit_spec("S-2", "Add a farewell.")
    with pytest.raises(ConflictError, match="cannot outlive"):
        await supervisor.directive("S-1", "D-1", DirectiveAction.CORRECT, "Use French.")


async def test_stop_and_wait_aborts_then_tells_the_implementer(
    supervisor: Supervisor, api: FakeOpenCode
) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    await supervisor.directive("S-1", "D-1", DirectiveAction.STOP_AND_WAIT, "The spec is wrong.")

    assert api.aborted == [record.session_id]
    prompt = api.prompts(record.session_id)[-1]
    assert prompt.startswith(directive_marker("D-1"))
    assert "The spec is wrong." in prompt


async def test_directives_are_idempotent_and_immutable(
    supervisor: Supervisor, api: FakeOpenCode
) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    await supervisor.directive("S-1", "D-1", DirectiveAction.CORRECT, "Use French.")
    await supervisor.directive("S-1", "D-1", DirectiveAction.CORRECT, "Use French.")
    assert len(api.prompts(record.session_id)) == 2

    with pytest.raises(ConflictError, match="other content"):
        await supervisor.directive("S-1", "D-1", DirectiveAction.CORRECT, "Use German.")


async def test_retracting_a_directive(supervisor: Supervisor, api: FakeOpenCode) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    await supervisor.directive("S-1", "D-1", DirectiveAction.CORRECT, "Use French.")
    directive = await supervisor.retract("S-1", "D-1")

    assert directive.retracted
    assert record.active_directives == []
    assert "D-1" in api.prompts(record.session_id)[-1]
    with pytest.raises(ConflictError, match="no directive"):
        await supervisor.retract("S-1", "D-9")


async def test_commands_need_an_active_spec(supervisor: Supervisor) -> None:
    with pytest.raises(NoActiveSpecError):
        await supervisor.say("hello")
    attention = supervisor.attention()
    assert attention is not None
    assert attention.reason is WaitReason.NO_SPEC


# ---- restarts --------------------------------------------------------------------


def seed_state(paths: TaskPaths, task_dir: Path, record: SpecRecord) -> None:
    state = SupervisorState(task_dir=str(task_dir), current_spec_id=record.spec_id)
    state.specs[record.spec_id] = record
    StateStore(paths.state, task_dir).save(state)


async def test_restart_does_not_resend_a_prompt_that_arrived(
    make_supervisor: SupervisorFactory, api: FakeOpenCode, paths: TaskPaths, task_dir: Path
) -> None:
    session = await api.create_session("t", [])
    await api.prompt(session.id, f"{spec_marker('S-1')}\n\nAdd a greeting.", None)
    seed_state(
        paths,
        task_dir,
        SpecRecord(
            spec_id="S-1",
            digest=digest("Add a greeting."),
            text="Add a greeting.",
            session_id=session.id,
            status=SpecStatus.PROMPT_PENDING,
        ),
    )

    supervisor = make_supervisor()
    await supervisor.start()

    assert len(api.prompts(session.id)) == 1
    assert supervisor.state.specs["S-1"].status is SpecStatus.ACTIVE


async def test_restart_sends_a_prompt_that_never_arrived(
    make_supervisor: SupervisorFactory, api: FakeOpenCode, paths: TaskPaths, task_dir: Path
) -> None:
    session = await api.create_session("t", [])
    seed_state(
        paths,
        task_dir,
        SpecRecord(
            spec_id="S-1",
            digest=digest("Add a greeting."),
            text="Add a greeting.",
            session_id=session.id,
            status=SpecStatus.PROMPT_PENDING,
        ),
    )

    supervisor = make_supervisor()
    await supervisor.start()

    [prompt] = api.prompts(session.id)
    assert prompt.startswith(spec_marker("S-1"))


async def test_restart_replaces_a_session_the_server_lost(
    make_supervisor: SupervisorFactory, api: FakeOpenCode, paths: TaskPaths, task_dir: Path
) -> None:
    seed_state(
        paths,
        task_dir,
        SpecRecord(
            spec_id="S-1",
            digest=digest("Add a greeting."),
            text="Add a greeting.",
            session_id="ses_gone",
            status=SpecStatus.ACTIVE,
        ),
    )

    supervisor = make_supervisor()
    await supervisor.start()

    record = supervisor.state.specs["S-1"]
    assert record.session_id != "ses_gone"
    assert record.lost_sessions == ["ses_gone"]
    assert api.prompts(record.session_id)[0].startswith(spec_marker("S-1"))
    assert "session_missing" in journal_actions(paths)


# ---- results and protected files ---------------------------------------------------


async def test_a_fresh_result_ends_the_spec(supervisor: Supervisor, paths: TaskPaths) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    write_result(supervisor)

    await supervisor.tick()

    assert record.status is SpecStatus.RESULT_WRITTEN
    # Reported once the turn is over, not while the model is still going.
    assert supervisor.attention() is None
    await supervisor.handle_event(event("session.idle", sessionID=record.session_id))
    attention = supervisor.attention()
    assert attention is not None
    assert (attention.reason, attention.detail) == (WaitReason.RESULT, "done")
    evidence = list(paths.evidence.glob("*/session.json"))
    assert len(evidence) == 1
    assert evidence[0].stat().st_mode & 0o777 == 0o600


async def test_a_stale_or_foreign_result_is_ignored(supervisor: Supervisor) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    path = write_result(supervisor)
    old = record.last_prompt_at - 60
    os.utime(path, (old, old))
    await supervisor.tick()
    assert record.status is SpecStatus.ACTIVE

    write_result(supervisor, spec_id="S-0")
    await supervisor.tick()
    assert record.status is SpecStatus.ACTIVE


async def test_a_correction_reopens_a_finished_spec(supervisor: Supervisor) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    path = write_result(supervisor)
    await supervisor.tick()
    assert record.status is SpecStatus.RESULT_WRITTEN

    await asyncio.sleep(0.01)
    await supervisor.directive("S-1", "D-1", DirectiveAction.CORRECT, "Use French.")
    assert record.status is SpecStatus.ACTIVE
    # The old result predates the correction, so it no longer counts.
    old = record.last_prompt_at - 1
    os.utime(path, (old, old))
    await supervisor.tick()
    assert record.status is SpecStatus.ACTIVE


async def test_editing_a_protected_file_aborts_the_session(
    supervisor: Supervisor, api: FakeOpenCode, task_dir: Path, paths: TaskPaths
) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    original = (task_dir / "instruction.md").read_text()
    (task_dir / "instruction.md").write_text("Make the tests pass, or delete them.\n")

    await supervisor.tick()

    assert record.status is SpecStatus.PROTECTED_VIOLATION
    assert record.session_id in api.aborted
    assert "protected_violation" in paths.notifications.read_text()
    with pytest.raises(ConflictError, match="restore them first"):
        await supervisor.say("carry on")

    (task_dir / "instruction.md").write_text(original)
    await supervisor.say("carry on")
    assert record.status is SpecStatus.ACTIVE


async def test_creating_a_protected_file_counts_as_a_change(
    make_supervisor: SupervisorFactory, task_dir: Path
) -> None:
    supervisor = make_supervisor(protected=("hint.md",))
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    (task_dir / "hint.md").write_text("Try harder.\n")
    await supervisor.tick()
    assert record.status is SpecStatus.PROTECTED_VIOLATION


async def test_a_restart_with_a_new_protect_list_is_not_a_violation(
    make_supervisor: SupervisorFactory, task_dir: Path, paths: TaskPaths
) -> None:
    """The bug this guards against: every newly protected file read as tampered."""
    (task_dir / "pytest.ini").write_text("[pytest]\n")
    first = make_supervisor(protected=("instruction.md",))
    await first.submit_spec("S-1", "Add a greeting.")

    restarted = make_supervisor(protected=("pytest.ini", "src/*.py"))
    await restarted.start()
    await restarted.tick()

    record = restarted.state.specs["S-1"]
    assert record.status is SpecStatus.ACTIVE
    assert record.protected_patterns == ["pytest.ini", "src/*.py"]
    assert "protected_rebaselined" in journal_actions(paths)
    await restarted.submit_spec("S-2", "Next.")

    # The new list is enforced from the new baseline on.
    (task_dir / "pytest.ini").write_text("[pytest]\naddopts = -k nothing\n")
    await restarted.tick()
    assert restarted.state.specs["S-2"].status is SpecStatus.PROTECTED_VIOLATION


async def test_tampering_under_the_old_list_survives_a_restart_with_a_new_one(
    make_supervisor: SupervisorFactory, task_dir: Path, paths: TaskPaths
) -> None:
    first = make_supervisor(protected=("instruction.md",))
    await first.submit_spec("S-1", "Add a greeting.")
    original = (task_dir / "instruction.md").read_text()
    (task_dir / "instruction.md").write_text("Delete the tests.\n")

    # Restarted before the monitor noticed, and no longer protecting that file:
    # the change must not become the next baseline.
    restarted = make_supervisor(protected=("other.md",))
    await restarted.start()
    await restarted.tick()

    record = restarted.state.specs["S-1"]
    assert record.status is SpecStatus.PROTECTED_VIOLATION
    assert record.protected_patterns == ["instruction.md"]
    assert "protected_reconcile_refused" in journal_actions(paths)
    with pytest.raises(ConflictError, match=r"instruction\.md"):
        await restarted.submit_spec("S-2", "Next.")

    (task_dir / "instruction.md").write_text(original)
    await restarted.submit_spec("S-2", "Next.")


async def test_a_new_file_under_an_old_glob_survives_a_restart(
    make_supervisor: SupervisorFactory, task_dir: Path
) -> None:
    (task_dir / "docs").mkdir()
    (task_dir / "docs" / "a.md").write_text("A\n")
    first = make_supervisor(protected=("docs/*.md",))
    await first.submit_spec("S-1", "Add a greeting.")
    (task_dir / "docs" / "b.md").write_text("Planted.\n")

    restarted = make_supervisor(protected=("other.md",))
    await restarted.start()
    await restarted.tick()

    assert restarted.state.specs["S-1"].status is SpecStatus.PROTECTED_VIOLATION


async def test_state_from_before_patterns_were_kept_is_checked_entry_by_entry(
    make_supervisor: SupervisorFactory,
) -> None:
    first = make_supervisor(protected=("instruction.md", "hint.md"))
    record = await first.submit_spec("S-1", "Add a greeting.")
    # What a state file written by an older tandem holds: a baseline, no patterns,
    # and the false violation the old comparison raised.
    record.protected_patterns = None
    record.status = SpecStatus.PROTECTED_VIOLATION
    first.store.save(first.state)

    restarted = make_supervisor(protected=("instruction.md", "pytest.ini"))
    await restarted.start()

    assert restarted.state.specs["S-1"].protected_patterns == ["instruction.md", "pytest.ini"]
    await restarted.submit_spec("S-2", "Next.")


# ---- waiting -----------------------------------------------------------------------


async def test_wait_reports_a_blocked_session_and_wakes_on_events(
    supervisor: Supervisor, api: FakeOpenCode, paths: TaskPaths
) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    waiter = asyncio.create_task(supervisor.wait(5))
    await asyncio.sleep(0)
    assert not waiter.done()

    await supervisor.handle_event(
        event(
            "permission.asked",
            id="per_1",
            sessionID=record.session_id,
            permission="edit",
            patterns=["instruction.md"],
        )
    )
    attention = await asyncio.wait_for(waiter, 1)

    assert attention.reason is WaitReason.BLOCKED
    assert "per_1" in attention.detail
    assert "per_1 wants edit" in paths.notifications.read_text()


async def test_wait_times_out_while_work_continues(supervisor: Supervisor) -> None:
    await supervisor.submit_spec("S-1", "Add a greeting.")
    attention = await supervisor.wait(0.05)
    assert attention.reason is WaitReason.TIMEOUT


async def test_a_turn_that_ends_without_a_result(supervisor: Supervisor) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    await supervisor.handle_event(event("session.idle", sessionID=record.session_id))
    attention = supervisor.attention()
    assert attention is not None
    assert attention.reason is WaitReason.TURN_ENDED


async def test_provider_errors_alert_but_aborts_do_not(
    supervisor: Supervisor, paths: TaskPaths
) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    aborted = {"name": "MessageAbortedError", "data": {"message": "aborted"}}
    await supervisor.handle_event(
        event("session.error", sessionID=record.session_id, error=aborted)
    )
    assert not paths.notifications.exists()
    attention = supervisor.attention()
    assert attention is not None
    assert attention.reason is WaitReason.TURN_ENDED

    await supervisor.say("try again")
    failed = {"name": "APIError", "data": {"message": "rate limited", "isRetryable": False}}
    await supervisor.handle_event(event("session.error", sessionID=record.session_id, error=failed))
    attention = supervisor.attention()
    assert attention is not None
    assert attention.reason is WaitReason.ERROR
    assert "rate limited" in paths.notifications.read_text()


async def test_events_for_other_sessions_are_ignored(supervisor: Supervisor) -> None:
    await supervisor.submit_spec("S-1", "Add a greeting.")
    await supervisor.handle_event(event("session.idle", sessionID="ses_other"))
    assert supervisor.attention() is None


async def test_a_reasoning_stall_is_detected_and_escalated(
    supervisor: Supervisor, clock: FakeClock, paths: TaskPaths
) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    for _ in range(8):  # 16 minutes of thinking out loud, no tool calls
        clock.advance(120)
        await supervisor.handle_event(
            event(
                "message.part.delta",
                sessionID=record.session_id,
                messageID="msg_1",
                partID="prt_1",
                field="text",
                delta="hmm",
            )
        )
    await supervisor.tick()

    attention = supervisor.attention()
    assert attention is not None
    assert attention.reason is WaitReason.REASONING_STALL
    assert "reasoning_stall" in paths.notifications.read_text()
    assert "activity" in journal_actions(paths)


async def test_resync_restores_pending_requests_missed_while_disconnected(
    supervisor: Supervisor, api: FakeOpenCode
) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    api.ask_permission(record.session_id)
    await supervisor.handle_event(event("server.connected"))
    await supervisor.tick()

    attention = supervisor.attention()
    assert attention is not None
    assert attention.reason is WaitReason.BLOCKED


async def test_resync_finds_a_subagent_blocked_while_disconnected(
    supervisor: Supervisor, api: FakeOpenCode
) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    child = api.add_subagent(record.session_id)
    stranger = api.add_subagent("ses_unrelated")
    api.ask_permission(child)
    api.ask_permission(stranger)
    await supervisor.resync()

    assert supervisor.tracker.family == {record.session_id, child}
    assert len(supervisor.tracker.pending) == 1
    attention = supervisor.attention()
    assert attention is not None
    assert attention.reason is WaitReason.BLOCKED


async def test_a_spec_can_choose_its_model_and_agent(
    supervisor: Supervisor, api: FakeOpenCode
) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.", model="openai/m", agent="plan")
    [(model, agent)] = api.prompt_targets
    assert model is not None
    assert (model.provider_id, model.model_id, agent) == ("openai", "m", "plan")
    status = supervisor.status()
    assert (status.model, status.agent) == ("openai/m", "plan")

    await supervisor.directive("S-1", "D-1", DirectiveAction.CORRECT, "Shorter.")
    assert api.prompt_targets[-1] == (model, "plan")
    assert record.model == "openai/m"


async def test_a_spec_id_cannot_change_its_model(supervisor: Supervisor) -> None:
    await supervisor.submit_spec("S-1", "Add a greeting.", model="openai/a")
    await supervisor.submit_spec("S-1", "Add a greeting.", model="openai/a")
    with pytest.raises(ConflictError, match="model or agent"):
        await supervisor.submit_spec("S-1", "Add a greeting.", model="openai/b")


async def test_replying_releases_the_session(supervisor: Supervisor, api: FakeOpenCode) -> None:
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    request_id = api.ask_permission(record.session_id)
    await supervisor.resync()
    await supervisor.reply_permission(request_id, "reject", "not that file")

    assert api.permission_replies == [(request_id, "reject", "not that file")]
    assert supervisor.attention() is None


async def test_shutdown_releases_waiters(supervisor: Supervisor) -> None:
    await supervisor.submit_spec("S-1", "Add a greeting.")
    waiter = asyncio.create_task(supervisor.wait(5))
    await asyncio.sleep(0)
    await supervisor.begin_shutdown()
    attention = await asyncio.wait_for(waiter, 1)
    assert attention.reason is WaitReason.STOPPED


async def test_cost_sums_every_spec(supervisor: Supervisor, api: FakeOpenCode) -> None:
    first = await supervisor.submit_spec("S-1", "Add a greeting.")
    second = await supervisor.submit_spec("S-2", "Add a farewell.")
    api.sessions[first.session_id].cost = 0.25
    api.sessions[first.session_id].tokens.input = 1000
    api.sessions[second.session_id].cost = 0.5
    api.sessions[second.session_id].tokens.input = 3000

    report = await supervisor.cost()

    assert [s.spec_id for s in report.specs] == ["S-1", "S-2"]
    assert report.total_cost == pytest.approx(0.75)
    assert report.total_tokens.input == 4000


async def test_state_survives_on_disk(supervisor: Supervisor, paths: TaskPaths) -> None:
    await supervisor.submit_spec("S-1", "Add a greeting.")
    saved = json.loads(paths.state.read_text())
    assert saved["current_spec_id"] == "S-1"
    assert paths.state.stat().st_mode & 0o777 == 0o600
    assert time.time() - saved["updated_at"] < 60


# ---- found in review --------------------------------------------------------------


async def test_a_new_spec_cannot_adopt_a_tampered_baseline(
    supervisor: Supervisor, task_dir: Path
) -> None:
    await supervisor.submit_spec("S-1", "Add a greeting.")
    (task_dir / "instruction.md").write_text("Delete the tests.\n")
    await supervisor.tick()

    with pytest.raises(ConflictError, match="restore them first"):
        await supervisor.submit_spec("S-2", "Add a farewell.")


async def test_a_lost_session_for_finished_work_is_not_redelivered(
    make_supervisor: SupervisorFactory, api: FakeOpenCode, paths: TaskPaths, task_dir: Path
) -> None:
    seed_state(
        paths,
        task_dir,
        SpecRecord(
            spec_id="S-1",
            digest=digest("Add a greeting."),
            text="Add a greeting.",
            session_id="ses_gone",
            status=SpecStatus.RESULT_WRITTEN,
        ),
    )
    supervisor = make_supervisor()
    await supervisor.start()

    record = supervisor.state.specs["S-1"]
    assert record.lost_sessions == ["ses_gone"]
    assert record.status is SpecStatus.RESULT_WRITTEN
    assert api.sessions == {}


async def test_stop_and_wait_lets_the_aborted_turn_end_before_the_next_prompt(
    make_supervisor: SupervisorFactory, api: FakeOpenCode
) -> None:
    supervisor = make_supervisor()
    supervisor.abort_settle = 5
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    sending = asyncio.create_task(
        supervisor.directive("S-1", "D-1", DirectiveAction.STOP_AND_WAIT, "Stop.")
    )
    await asyncio.sleep(0.01)
    assert not sending.done()  # held until the abort's idle arrives
    assert len(api.prompts(record.session_id)) == 1

    await supervisor.handle_event(event("session.idle", sessionID=record.session_id))
    await asyncio.wait_for(sending, 1)

    assert len(api.prompts(record.session_id)) == 2
    assert supervisor.tracker.busy  # the new turn, not ended by the old idle
