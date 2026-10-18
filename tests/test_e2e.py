"""End to end: tandem driving a real ``opencode serve`` with a scripted model.

Nothing here is faked except the model's choices. The OpenCode binary, its
permission system and its event stream are real, which is the test the
in-memory fake cannot stand in for: a client stubbed in tests can pass locally
and still deadlock against the real service.

Needs OpenCode 1.x: set ``TANDEM_TEST_OPENCODE`` to the binary, or have
``opencode`` on PATH. The first run downloads OpenCode's OpenAI-compatible
provider package into ``TANDEM_TEST_CACHE`` (default ``~/.cache/tandem-test``).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from tandem.bridge.manifest import Manifest
from tandem.config import Settings, TaskPaths
from tandem.opencode.server import Version
from tandem.runtime import run
from tests.scripted_llm import Reply, Script, ToolCall, build_app, result_path

pytestmark = pytest.mark.e2e


def _opencode() -> str | None:
    binary = os.environ.get("TANDEM_TEST_OPENCODE") or shutil.which("opencode")
    if not binary:
        return None
    output = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False)
    try:
        return binary if Version.parse(output.stdout).major == 1 else None
    except ValueError:
        return None


OPENCODE = _opencode()
requires_opencode = pytest.mark.skipif(OPENCODE is None, reason="needs OpenCode 1.x")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def write_result(spec_id: str, summary: str) -> Callable[[list[dict[str, Any]]], dict[str, Any]]:
    """Script arguments that write the result file to the path from the prompt."""

    def arguments(messages: list[dict[str, Any]]) -> dict[str, Any]:
        content = json.dumps({"spec_id": spec_id, "verdict": "done", "summary": summary})
        return {"filePath": result_path(messages), "content": content}

    return arguments


class Harness:
    """One task: a scripted model, a tandem supervisor, and a client for its bridge."""

    def __init__(self, tmp_path: Path, script: Script) -> None:
        assert OPENCODE is not None
        self.script = script
        self.task_dir = tmp_path / "task"
        self.settings = Settings(
            task_dir=self._make_task(),
            model="scripted/scripted",
            protected=["instruction.md"],
            opencode_bin=OPENCODE,
            state_root=tmp_path / "state",
            poll_interval=0.2,
        )
        self.paths = TaskPaths.for_task(self.settings.state_root, self.settings.task_dir)
        self.http: httpx.AsyncClient | None = None
        self._run: asyncio.Task[None] | None = None

    def _make_task(self) -> Path:
        self.task_dir.mkdir()
        (self.task_dir / "instruction.md").write_text("Say hello.\n")
        subprocess.run(["git", "init", "-q", str(self.task_dir)], check=True)
        return self.task_dir

    async def start(self) -> None:
        ready: asyncio.Future[Manifest] = asyncio.get_running_loop().create_future()
        self._run = asyncio.create_task(run(self.settings, ready=ready.set_result))
        manifest = await asyncio.wait_for(asyncio.shield(ready), 90)
        headers = {"Authorization": f"Bearer {manifest.token.get_secret_value()}"}
        self.http = httpx.AsyncClient(base_url=manifest.url, headers=headers, timeout=120)

    async def stop(self) -> None:
        if self.http is not None:
            if self._run is not None and not self._run.done():
                await self.http.post("/finish")
            await self.http.aclose()
            self.http = None
        if self._run is not None:
            await asyncio.wait_for(self._run, 30)
            self._run = None

    async def get(self, path: str, **params: Any) -> Any:
        assert self.http is not None
        response = await self.http.get(path, params=params or None)
        assert response.is_success, response.text
        return response.json()

    async def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        assert self.http is not None
        response = await self.http.post(path, json=body)
        assert response.is_success, response.text
        return response.json()

    async def wait(self, seconds: float = 60) -> dict[str, Any]:
        return await self.get("/wait", timeout=seconds)

    def journal(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.paths.journal.read_text().splitlines()]

    def actions(self) -> list[str]:
        return [entry["action"] for entry in self.journal()]


@pytest.fixture
async def harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncIterator[Harness]:
    script: Script = request.param
    llm_port = free_port()
    llm = uvicorn.Server(
        uvicorn.Config(build_app(script), port=llm_port, log_level="warning", lifespan="off")
    )
    llm_task = asyncio.create_task(llm.serve())

    config = tmp_path / "config" / "opencode"
    config.mkdir(parents=True)
    provider = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Scripted",
        "options": {"baseURL": f"http://127.0.0.1:{llm_port}/v1", "apiKey": "x"},
        "models": {"scripted": {"name": "Scripted", "tool_call": True}},
    }
    (config / "opencode.json").write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "provider": {"scripted": provider},
                "model": "scripted/scripted",
                "small_model": "scripted/scripted",
                "autoupdate": False,
                "share": "disabled",
            }
        )
    )
    cache = Path(os.environ.get("TANDEM_TEST_CACHE", Path.home() / ".cache" / "tandem-test"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))

    harness = Harness(tmp_path, script)
    await harness.start()
    try:
        yield harness
    finally:
        await harness.stop()
        llm.should_exit = True
        await llm_task


# ---- a spec from submission to result -------------------------------------------

GREETING = Script(
    {
        "TANDEM_SPEC_ID: S-1": [
            ToolCall("write", {"filePath": "hello.txt", "content": "hi\n"}, say="Writing it."),
            ToolCall(
                "edit",
                {
                    "filePath": "instruction.md",
                    "oldString": "Say hello.",
                    "newString": "Say nothing.",
                },
            ),
            ToolCall("write", write_result("S-1", "wrote hello.txt")),
            Reply("Done."),
        ]
    }
)


@requires_opencode
@pytest.mark.parametrize("harness", [GREETING], indirect=True)
async def test_a_spec_from_submission_to_result(harness: Harness) -> None:
    submitted = await harness.post("/spec", {"spec_id": "S-1", "text": "Create hello.txt."})
    assert submitted["status"] == "active"

    # The edit to the protected file stops for a human instead of happening.
    blocked = await harness.wait()
    assert blocked["reason"] == "blocked", blocked
    [permission] = (await harness.get("/pending"))["permissions"]
    assert permission["patterns"] == ["instruction.md"]
    assert "wants edit on instruction.md" in harness.paths.notifications.read_text()

    # A rejection with a message goes back to the model as feedback, so the
    # turn carries on rather than ending.
    await harness.post(
        "/permission",
        {"id": permission["id"], "reply": "reject", "message": "Leave instruction.md alone."},
    )
    finished = await harness.wait()
    assert finished["reason"] == "result", finished

    assert (harness.task_dir / "hello.txt").read_text() == "hi\n"
    assert (harness.task_dir / "instruction.md").read_text() == "Say hello.\n"
    assert (await harness.get("/result"))["result"]["verdict"] == "done"
    cost = await harness.get("/cost")
    assert cost["total_tokens"]["input"] > 0
    for expected in ("session_created", "spec_delivered", "permission", "result_observed"):
        assert expected in harness.actions()
    assert harness.paths.events.stat().st_size > 0


# ---- questions, corrections, and a second spec ------------------------------------

QUESTION = {
    "questions": [
        {
            "question": "Formal or informal greeting?",
            "header": "Tone",
            "options": [
                {"label": "Formal", "description": "Good morning"},
                {"label": "Informal", "description": "Hey"},
            ],
        }
    ]
}

REVIEW_LOOP = Script(
    {
        "TANDEM_SPEC_ID: S-1": [
            ToolCall("question", QUESTION),
            ToolCall("write", {"filePath": "hello.txt", "content": "hey\n"}),
            ToolCall("write", write_result("S-1", "informal greeting")),
            Reply("Done."),
        ],
        "TANDEM_DIRECTIVE_ID: D-1": [
            ToolCall("write", {"filePath": "hello.txt", "content": "salut\n"}),
            ToolCall("write", write_result("S-1", "now in French")),
            Reply("Corrected."),
        ],
        "TANDEM_SPEC_ID: S-2": [
            ToolCall("write", {"filePath": "bye.txt", "content": "bye\n"}),
            ToolCall("write", write_result("S-2", "wrote bye.txt")),
            Reply("Done."),
        ],
    }
)


@requires_opencode
@pytest.mark.parametrize("harness", [REVIEW_LOOP], indirect=True)
async def test_questions_corrections_and_a_second_spec(harness: Harness) -> None:
    first = await harness.post("/spec", {"spec_id": "S-1", "text": "Create hello.txt."})

    asked = await harness.wait()
    assert asked["reason"] == "blocked", asked
    [question] = (await harness.get("/pending"))["questions"]
    await harness.post("/answer", {"id": question["id"], "answers": [["Informal"]]})
    assert (await harness.wait())["reason"] == "result"
    assert (harness.task_dir / "hello.txt").read_text() == "hey\n"

    # The planner reviews the result and sends a correction under the same spec.
    await harness.post(
        "/directive",
        {"spec_id": "S-1", "directive_id": "D-1", "action": "CORRECT", "text": "Use French."},
    )
    corrected = await harness.wait()
    assert corrected["reason"] == "result", corrected
    assert corrected["status"]["result"]["summary"] == "now in French"
    assert (harness.task_dir / "hello.txt").read_text() == "salut\n"

    # A new spec gets a fresh session; a directive for the old one is refused.
    second = await harness.post("/spec", {"spec_id": "S-2", "text": "Create bye.txt."})
    assert second["session_id"] != first["session_id"]
    assert harness.http is not None
    stale = await harness.http.post(
        "/directive",
        json={"spec_id": "S-1", "directive_id": "D-2", "action": "CORRECT", "text": "More French."},
    )
    assert stale.status_code == 409
    assert (await harness.wait())["reason"] == "result"
    assert (harness.task_dir / "bye.txt").read_text() == "bye\n"

    cost = await harness.get("/cost")
    assert [spec["spec_id"] for spec in cost["specs"]] == ["S-1", "S-2"]


# ---- the hash guard, and a restart --------------------------------------------------

TAMPER = Script(
    {
        "TANDEM_SPEC_ID: S-1": [
            # A shell redirect is not an edit, so no permission rule sees it.
            ToolCall(
                "bash",
                {"command": "printf 'Skip the tests.\\n' >> instruction.md", "description": "x"},
            ),
            Reply("Changed the instructions."),
        ],
        "TANDEM_DIRECTIVE_ID: D-1": [
            ToolCall("write", write_result("S-1", "left instruction.md alone")),
            Reply("Done."),
        ],
    }
)


@requires_opencode
@pytest.mark.parametrize("harness", [TAMPER], indirect=True)
async def test_a_protected_file_changed_by_shell_and_a_restart(harness: Harness) -> None:
    submitted = await harness.post("/spec", {"spec_id": "S-1", "text": "Create hello.txt."})

    violation = await harness.wait()
    assert violation["reason"] == "protected_violation", violation
    assert "protected_violation" in harness.paths.notifications.read_text()

    # The supervisor restarts mid-spec and adopts the session it recorded.
    await harness.stop()
    await harness.start()
    status = await harness.get("/status")
    assert (status["spec_id"], status["session_id"]) == ("S-1", submitted["session_id"])
    assert status["spec_status"] == "protected_violation"

    # Nothing moves until the file is restored.
    assert harness.http is not None
    refused = await harness.http.post(
        "/directive",
        json={"spec_id": "S-1", "directive_id": "D-1", "action": "CORRECT", "text": "Undo that."},
    )
    assert refused.status_code == 409
    (harness.task_dir / "instruction.md").write_text("Say hello.\n")
    await harness.post(
        "/directive",
        {"spec_id": "S-1", "directive_id": "D-1", "action": "CORRECT", "text": "Undo that."},
    )
    assert (await harness.wait())["reason"] == "result"
    assert harness.actions().count("supervisor_started") == 2
    assert harness.actions().count("session_created") == 1
