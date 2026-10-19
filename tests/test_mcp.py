"""The MCP tools against a mocked bridge: finding the project, replies, errors."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import SecretStr

from tandem import mcp_server
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
