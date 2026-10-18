"""State, locks, journal, protected files, the event recorder, and the manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from tandem.bridge.manifest import Manifest
from tandem.config import TaskPaths
from tandem.opencode.events import Event, StreamRecorder
from tandem.supervisor.journal import Journal
from tandem.supervisor.protected import MISSING, ProtectedFiles
from tandem.supervisor.state import (
    SpecRecord,
    StateError,
    StateStore,
    SupervisorLock,
    SupervisorState,
    write_private,
)


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_state_round_trips(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json", Path("/work/task"))
    state = SupervisorState(task_dir="/work/task", current_spec_id="S-1")
    state.specs["S-1"] = SpecRecord(spec_id="S-1", digest="d", text="t", session_id="ses_1")
    store.save(state)

    loaded = store.load()
    assert loaded.current is not None
    assert loaded.current.session_id == "ses_1"
    assert mode(store.path) == 0o600


def test_state_from_another_task_is_refused(tmp_path: Path) -> None:
    StateStore(tmp_path / "state.json", Path("/a")).save(SupervisorState(task_dir="/a"))
    with pytest.raises(StateError, match="belongs to /a"):
        StateStore(tmp_path / "state.json", Path("/b")).load()


def test_write_private_leaves_no_temporary_files(tmp_path: Path) -> None:
    write_private(tmp_path / "x.json", "{}")
    write_private(tmp_path / "x.json", '{"a": 1}')
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]
    assert json.loads((tmp_path / "x.json").read_text()) == {"a": 1}


def test_one_supervisor_per_task(tmp_path: Path) -> None:
    with (
        SupervisorLock(tmp_path / "supervisor.lock"),
        pytest.raises(RuntimeError, match="already owns"),
    ):
        SupervisorLock(tmp_path / "supervisor.lock").__enter__()
    with SupervisorLock(tmp_path / "supervisor.lock"):
        pass  # released on exit


def test_journal_appends_with_time_and_action_last(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "journal.jsonl")
    journal.append("prompt", time="spoofed", characters=12)
    journal.append("abort")

    first, second = (json.loads(line) for line in journal.path.read_text().splitlines())
    assert isinstance(first["time"], float)  # a detail cannot redefine the schema
    assert first["action"] == "prompt"
    assert list(second) == ["time", "action"]
    assert len(journal.tail(1)) == 1
    assert mode(journal.path) == 0o600


def test_protected_snapshot_and_changes(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("a")
    files = ProtectedFiles(tmp_path, ("docs/*.md", "hint.md"))
    baseline = files.snapshot()
    assert baseline["hint.md"] == MISSING
    assert files.changes(baseline) == []

    (tmp_path / "docs" / "b.md").write_text("b")
    (tmp_path / "docs" / "a.md").write_text("changed")
    assert files.changes(baseline) == ["docs/a.md", "docs/b.md"]


def test_recorder_stubs_oversized_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.sse"
    huge = json.dumps({"id": "evt_1", "type": "message.updated", "properties": {"x": "y" * 500}})
    with StreamRecorder(path, max_line=200) as recorder:
        recorder.write('{"type":"session.idle","properties":{}}', flush=True)
        recorder.write(huge)

    lines = path.read_text().splitlines()
    assert lines[0] == 'data: {"type":"session.idle","properties":{}}'
    stub = json.loads(lines[1].removeprefix("data: "))
    assert stub == {"type": "message.updated", "tandemDropped": True, "bytes": len(huge)}
    assert recorder.dropped == 1
    assert mode(path) == 0o600


def test_event_accessors() -> None:
    asked = Event.parse(
        '{"id":"evt_1","type":"permission.asked","properties":{"id":"per_1","sessionID":"ses_1"}}'
    )
    assert asked is not None
    assert (asked.session_id, asked.request_id) == ("ses_1", "per_1")

    part = Event.parse(
        '{"type":"message.part.updated","properties":{"part":{"sessionID":"ses_2"}}}'
    )
    assert part is not None
    assert part.session_id == "ses_2"

    error = Event.parse(
        '{"type":"session.error","properties":'
        '{"error":{"name":"APIError","data":{"message":"429"}}}}'
    )
    assert error is not None
    assert (error.error_name, error.error_message) == ("APIError", "429")

    assert Event.parse("not json") is None
    assert Event.parse('{"no":"type"}') is None


def test_manifest_keeps_the_token_private(tmp_path: Path) -> None:
    manifest = Manifest(
        host="127.0.0.1",
        port=1234,
        token=SecretStr("s3cret"),
        task_dir="/t",
        state_dir=str(tmp_path),
    )
    path = tmp_path / "manifest.json"
    manifest.write(path)

    assert mode(path) == 0o600
    assert Manifest.read(path).token.get_secret_value() == "s3cret"
    assert "s3cret" not in repr(manifest)
    assert manifest.alive
    Manifest.remove(path)
    assert not path.exists()


def test_task_paths_are_stable_and_distinct(tmp_path: Path) -> None:
    one = TaskPaths.for_task(tmp_path, Path("/a/task"))
    two = TaskPaths.for_task(tmp_path, Path("/b/task"))
    assert one == TaskPaths.for_task(tmp_path, Path("/a/task"))
    assert one.root != two.root
    assert one.root.name.startswith("task-")
