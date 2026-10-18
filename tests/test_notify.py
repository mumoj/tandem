from __future__ import annotations

from pathlib import Path

import httpx
import respx

from tandem.notify import Alert, Notifier
from tests.fakes import event


def test_only_blocking_events_become_alerts() -> None:
    asked = Alert.from_event(
        event(
            "permission.asked",
            id="per_1",
            sessionID="ses_1",
            permission="edit",
            patterns=["instruction.md"],
        )
    )
    assert asked is not None
    assert "per_1 wants edit on instruction.md" in asked.summary

    question = Alert.from_event(
        event(
            "question.asked",
            id="que_1",
            sessionID="ses_1",
            questions=[{"question": "Which database?", "header": "DB", "options": []}],
        )
    )
    assert question is not None
    assert "Which database?" in question.summary

    aborted = {"name": "MessageAbortedError", "data": {"message": "aborted"}}
    assert Alert.from_event(event("session.error", sessionID="ses_1", error=aborted)) is None
    assert Alert.from_event(event("session.idle", sessionID="ses_1")) is None


async def test_the_command_gets_the_summary_in_its_environment(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    notifier = Notifier(
        log_path=tmp_path / "notifications.log",
        task="demo",
        state_dir=tmp_path,
        command=f'printf "%s|%s|%s" "$TANDEM_KIND" "$TANDEM_TASK" "$TANDEM_NOTIFY" > {out}',
    )
    notifier.escalate(Alert("permission.asked", "per_1 wants edit", "ses_1", {"type": "x"}))
    await notifier.drain()

    assert out.read_text() == "permission.asked|demo|per_1 wants edit"
    assert "per_1 wants edit" in (tmp_path / "notifications.log").read_text()


async def test_a_hung_command_is_killed_not_awaited_forever(tmp_path: Path) -> None:
    notifier = Notifier(
        log_path=tmp_path / "notifications.log",
        task="demo",
        state_dir=tmp_path,
        command="sleep 30",
        timeout=0.2,
    )
    notifier.escalate(Alert("hung", "nothing for 300s"))
    await notifier.drain()  # returns because the command was killed at 0.2s


async def test_a_failing_command_is_logged_not_raised(tmp_path: Path) -> None:
    notifier = Notifier(
        log_path=tmp_path / "n.log", task="demo", state_dir=tmp_path, command="exit 3"
    )
    notifier.escalate(Alert("hung", "x"))
    await notifier.drain()


@respx.mock
async def test_ntfy_is_opt_in(tmp_path: Path) -> None:
    route = respx.post("https://ntfy.example/topic-abc").respond(200)
    async with httpx.AsyncClient() as http:
        silent = Notifier(log_path=tmp_path / "a.log", task="demo", state_dir=tmp_path, http=http)
        silent.escalate(Alert("hung", "x"))
        await silent.drain()
        assert not route.called

        loud = Notifier(
            log_path=tmp_path / "b.log",
            task="demo",
            state_dir=tmp_path,
            ntfy_topic="topic-abc",
            ntfy_server="https://ntfy.example/",
            http=http,
        )
        loud.escalate(Alert("blocked", "per_1 wants edit"))
        await loud.drain()

    assert route.calls.last.request.content == b"per_1 wants edit"
    assert route.calls.last.request.headers["title"] == "tandem demo: blocked"
