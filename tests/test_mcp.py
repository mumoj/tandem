"""The MCP tools against a mocked bridge: finding the project, replies, errors."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import SecretStr

from tandem import mcp_server, worktrees
from tandem.bridge.manifest import Manifest
from tandem.config import TaskPaths

URL = "http://127.0.0.1:41999"

STATUS = {
    "task_dir": "/work/demo",
    "spec_id": "S-1",
    "session_id": "ses_1",
    "spec_status": "active",
    "model": "openai/m",
    "agent": None,
    "activity": {
        "activity": "blocked",
        "busy": True,
        "seconds_since_tool": 40,
        "seconds_since_text": 12,
        "running_tools": [],
        "pending": ["per_1"],
        "error": None,
        "retry": None,
    },
    "active_directives": [],
    "result": None,
}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project directory the MCP server was started in, with a supervisor running."""
    state_root = tmp_path / "state"
    task_dir = (tmp_path / "demo").resolve()
    task_dir.mkdir()
    monkeypatch.setenv("TANDEM_STATE_ROOT", str(state_root))
    monkeypatch.setenv("TANDEM_WORKDIR", str(task_dir))
    paths = TaskPaths.for_task(state_root, task_dir)
    Manifest(
        host="127.0.0.1",
        port=41999,
        token=SecretStr("tok"),
        task_dir=str(task_dir),
        state_dir=str(paths.root),
        pid=os.getpid(),
    ).write(paths.manifest)
    return task_dir


@respx.mock
def test_submit_spec_finds_the_project_and_returns_the_dashboard(project: Path) -> None:
    route = respx.post(f"{URL}/spec").respond(
        json={"spec_id": "S-1", "session_id": "ses_1", "status": "active", "result_path": "/r"}
    )
    out = mcp_server.submit_spec("S-1", "Build it.", model="openai/m")

    assert json.loads(route.calls.last.request.content) == {
        "spec_id": "S-1",
        "text": "Build it.",
        "model": "openai/m",
    }
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"
    assert out["dashboard"] == f"{URL}/ui#token=tok"


@respx.mock
def test_wait_says_what_to_do_next(project: Path) -> None:
    respx.get(f"{URL}/wait").respond(json={"reason": "blocked", "detail": "d", "status": STATUS})
    out = mcp_server.wait(timeout=5)
    assert out["reason"] == "blocked"
    assert "get_pending" in out["next_step"]


@respx.mock
def test_reply_permission_picks_the_only_request(project: Path) -> None:
    pending = {
        "permissions": [{"id": "per_1", "sessionID": "ses_1", "permission": "edit"}],
        "questions": [],
    }
    respx.get(f"{URL}/pending").respond(json=pending)
    route = respx.post(f"{URL}/permission").respond(json={"detail": "per_1 reject"})

    assert mcp_server.reply_permission("deny", message="not that file") == "per_1 reject"
    assert json.loads(route.calls.last.request.content) == {
        "id": "per_1",
        "reply": "reject",
        "message": "not that file",
    }


@respx.mock
def test_bridge_errors_become_tool_errors(project: Path) -> None:
    respx.post(f"{URL}/spec").respond(409, json={"detail": "Spec ID S-1 is immutable"})
    with pytest.raises(ToolError, match="immutable"):
        mcp_server.submit_spec("S-1", "Something else.")


def test_nothing_running_says_how_to_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TANDEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("TANDEM_WORKDIR", str(tmp_path))
    with pytest.raises(ToolError, match="submit_spec starts one"):
        mcp_server.status()


def test_a_missing_workdir_is_refused(project: Path) -> None:
    with pytest.raises(ToolError, match="no such directory"):
        mcp_server.status(workdir="does-not-exist")


def test_answer_needs_exactly_one_of_answers_or_reject(project: Path) -> None:
    with pytest.raises(ToolError, match="either"):
        mcp_server.answer_question()


async def test_the_server_lists_every_tool_with_instructions() -> None:
    tools = {tool.name for tool in await mcp_server.server.list_tools()}
    assert {
        "submit_spec",
        "wait",
        "get_result",
        "get_pending",
        "reply_permission",
        "dashboard",
        "list_models",
        "list_implementers",
    } <= tools
    assert "dashboard link" in (mcp_server.server.instructions or "")


# ---- parallel implementers ------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    worktrees.git(root, "init", "-q", "-b", "main")
    worktrees.git(root, "config", "user.email", "dev@example.test")
    worktrees.git(root, "config", "user.name", "dev")
    (root / "app.py").write_text("x = 1\n")
    worktrees.git(root, "add", "-A")
    worktrees.git(root, "commit", "-qm", "init")
    monkeypatch.setenv("TANDEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("TANDEM_WORKDIR", str(root))
    return root


def running(worktree: worktrees.Worktree, port: int) -> Manifest:
    """Pretend a supervisor runs in ``worktree``: a live manifest at ``port``."""
    paths = TaskPaths.for_task(mcp_server._state_root(), worktree.path)  # pyright: ignore[reportPrivateUsage]
    manifest = Manifest(
        host="127.0.0.1",
        port=port,
        token=SecretStr("tok"),
        task_dir=str(worktree.path),
        state_dir=str(paths.root),
        pid=os.getpid(),
    )
    manifest.write(paths.manifest)
    return manifest


def overview(attention: str | None, spec_id: str = "S-1") -> dict[str, Any]:
    status = STATUS | {"spec_id": spec_id}
    return {
        "status": status,
        "attention": attention,
        "attention_detail": "done" if attention else "",
        "pending": {"permissions": [], "questions": []},
    }


class Announcements:
    """Stands in for the MCP context: records what wait_any reports as progress."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.messages.append(message or "")


def spawn(repo: Path, name: str) -> worktrees.Worktree:
    return worktrees.create_worktree(repo, mcp_server._state_root(), name, None)  # pyright: ignore[reportPrivateUsage]


@respx.mock
async def test_wait_any_returns_when_one_finishes_and_announces_it(repo: Path) -> None:
    running(spawn(repo, "alpha"), 42001)
    running(spawn(repo, "beta"), 42002)
    respx.get("http://127.0.0.1:42001/overview").respond(json=overview(None))
    respx.get("http://127.0.0.1:42002/overview").mock(
        side_effect=[
            httpx.Response(200, json=overview(None)),
            httpx.Response(200, json=overview("result")),
        ]
    )
    ctx = Announcements()

    out = await mcp_server.wait_any(cast(Context, ctx), timeout=30)

    assert out.reason == "ready"
    assert [(s.name, s.state) for s in out.ready] == [("beta", "result")]
    assert "get_result" in out.ready[0].next_step
    assert out.working == ["alpha"]
    assert ctx.messages == ["beta finished S-1: result"]


async def test_wait_any_reports_stopped_implementers_instead_of_hanging(repo: Path) -> None:
    spawn(repo, "gone")
    out = await mcp_server.wait_any(cast(Context, Announcements()), timeout=30)
    assert out.reason == "all_stopped"
    assert out.stopped == ["gone"]


async def test_wait_any_without_implementers_says_how_to_start(repo: Path) -> None:
    with pytest.raises(ToolError, match="spawn_implementer first"):
        await mcp_server.wait_any(cast(Context, Announcements()), timeout=1)


@respx.mock
def test_every_tool_takes_an_implementer_name_as_workdir(repo: Path) -> None:
    running(spawn(repo, "alpha"), 42001)
    respx.get("http://127.0.0.1:42001/status").respond(json=STATUS)
    assert mcp_server.status(workdir="alpha")["dashboard"].startswith("http://127.0.0.1:42001")
    with pytest.raises(ToolError, match="no such directory or implementer"):
        mcp_server.status(workdir="nobody")


@respx.mock
def test_spawn_creates_a_worktree_starts_it_and_submits_the_spec(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Path] = []

    def fake_spawn(task_dir: Path, model: str | None, protect: list[str] | None) -> Manifest:
        started.append(task_dir)
        return running(worktrees.Worktree("alpha", task_dir, "tandem/alpha"), 42001)

    monkeypatch.setattr(mcp_server, "_spawn", fake_spawn)
    route = respx.post("http://127.0.0.1:42001/spec").respond(
        json={"spec_id": "S-1", "session_id": "ses_1", "status": "active", "result_path": "/r"}
    )

    out = mcp_server.spawn_implementer("alpha", "Add a farewell.", model="openai/m")

    [tree] = worktrees.list_worktrees(repo)
    assert started == [tree.path]
    assert out["branch"] == "tandem/alpha"
    assert out["worktree"] == str(tree.path)
    assert out["note"] == ""
    assert json.loads(route.calls.last.request.content)["model"] == "openai/m"

    # Retrying reuses the worktree and the running supervisor.
    mcp_server.spawn_implementer("alpha", "Add a farewell.", model="openai/m")
    assert len(started) == 1
    assert len(worktrees.list_worktrees(repo)) == 1


def test_land_reviews_then_merges_without_a_supervisor(repo: Path) -> None:
    tree = spawn(repo, "alpha")
    (tree.path / "farewell.py").write_text("def farewell(name):\n    return name\n")

    review = mcp_server.land_implementer("alpha")
    assert "+def farewell(name):" in review["diff"]
    assert review["running"] is False

    merged = mcp_server.land_implementer("alpha", "merge", message="Add farewell")
    assert merged["merged_into"] == "main"
    assert merged["committed_leftovers"] is True
    assert (repo / "farewell.py").is_file()
    assert worktrees.list_worktrees(repo) == []


@respx.mock
def test_land_refuses_to_merge_a_busy_implementer(repo: Path) -> None:
    running(spawn(repo, "alpha"), 42001)
    activity = cast(dict[str, Any], STATUS["activity"]) | {"activity": "working", "busy": True}
    busy = STATUS | {"activity": activity}
    respx.get("http://127.0.0.1:42001/status").respond(json=busy)
    with pytest.raises(ToolError, match="still working"):
        mcp_server.land_implementer("alpha", "merge")
    assert len(worktrees.list_worktrees(repo)) == 1


def test_a_new_implementer_with_an_old_name_does_not_inherit_its_state(repo: Path) -> None:
    """Regression: the old spec read as already submitted, so no work was sent."""
    tree = spawn(repo, "alpha")
    state = TaskPaths.for_task(mcp_server._state_root(), tree.path).root  # pyright: ignore[reportPrivateUsage]
    state.mkdir(parents=True)
    (state / "state.json").write_text("{}")

    out = mcp_server.land_implementer("alpha", "discard")

    assert out["discarded"] is True
    assert out["evidence"] is not None
    assert (Path(out["evidence"]) / "state.json").is_file()
    assert not state.exists()


def test_leftover_state_is_archived_when_a_fresh_worktree_takes_its_place(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = worktrees.worktree_home(mcp_server._state_root(), repo) / "alpha"  # pyright: ignore[reportPrivateUsage]
    stale = TaskPaths.for_task(mcp_server._state_root(), path).root  # pyright: ignore[reportPrivateUsage]
    stale.mkdir(parents=True)
    (stale / "state.json").write_text("{}")

    def fake_spawn(task_dir: Path, model: str | None, protect: list[str] | None) -> Manifest:
        assert not stale.exists(), "the supervisor must start without the old state"
        raise ToolError("stop here")

    monkeypatch.setattr(mcp_server, "_spawn", fake_spawn)
    with pytest.raises(ToolError, match="stop here"):
        mcp_server.spawn_implementer("alpha", "Add a farewell.")
    assert list((mcp_server._state_root() / "landed").iterdir())  # pyright: ignore[reportPrivateUsage]
