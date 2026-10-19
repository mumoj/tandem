"""The CLI against a mocked bridge: discovery, rendering and exit codes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import respx
from pydantic import SecretStr
from typer.testing import CliRunner

from tandem.bridge.manifest import Manifest
from tandem.cli import app

URL = "http://127.0.0.1:41999"
runner = CliRunner()


def write_manifest(
    root: Path, name: str, *, pid: int | None = None, opencode_password: str | None = "ocpw"
) -> Manifest:
    manifest = Manifest(
        host="127.0.0.1",
        port=41999,
        token=SecretStr("tok"),
        task_dir=f"/work/{name}",
        state_dir=str(root / name),
        opencode_port=4096,
        opencode_version="1.19.3",
        opencode_password=SecretStr(opencode_password) if opencode_password else None,
        pid=pid or os.getpid(),
    )
    manifest.write(root / name / "manifest.json")
    return manifest


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    write_manifest(root, "demo-abc123")
    return root


def invoke(state_root: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["--state-root", str(state_root), *args])
    return result.exit_code, result.output


STATUS = {
    "task_dir": "/work/demo",
    "spec_id": "S-1",
    "session_id": "ses_1",
    "spec_status": "active",
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


@respx.mock
def test_status_finds_the_only_running_supervisor(state_root: Path) -> None:
    route = respx.get(f"{URL}/status").respond(json=STATUS)
    code, output = invoke(state_root, "status")

    assert code == 0
    assert "S-1  [active]" in output
    assert "pending   per_1" in output
    assert route.calls.last.request.headers["authorization"] == "Bearer tok"


@respx.mock
def test_json_output_is_the_raw_response(state_root: Path) -> None:
    respx.get(f"{URL}/status").respond(json=STATUS)
    code, output = invoke(state_root, "status", "--json")
    assert code == 0
    assert json.loads(output)["activity"]["pending"] == ["per_1"]


@respx.mock
def test_attach_hands_the_terminal_to_opencode(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(f"{URL}/status").respond(json=STATUS)
    calls: list[tuple[str, list[str], dict[str, str]]] = []
    monkeypatch.setattr(os, "execvpe", lambda *call: calls.append(call))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]

    code, _ = invoke(state_root, "attach")

    assert code == 0
    ((binary, argv, env),) = calls
    assert binary == "opencode"
    assert argv == [
        "opencode",
        "attach",
        "http://127.0.0.1:4096",
        "--dir",
        "/work/demo-abc123",
        "--session",
        "ses_1",
    ]
    assert env["OPENCODE_SERVER_PASSWORD"] == "ocpw"
    assert "ocpw" not in argv


@respx.mock
def test_attach_web_prints_the_login(state_root: Path) -> None:
    respx.get(f"{URL}/status").respond(json=STATUS)
    code, output = invoke(state_root, "attach", "--web")
    assert code == 0
    assert "url      http://127.0.0.1:4096" in output
    assert "password ocpw" in output
    assert "session  ses_1" in output


@respx.mock
def test_attach_to_an_older_supervisor_says_restart(tmp_path: Path) -> None:
    write_manifest(tmp_path, "old-abc123", opencode_password=None)
    respx.get(f"{URL}/status").respond(json=STATUS)
    code, output = invoke(tmp_path, "attach")
    assert code == 1
    assert "restart it" in output


@pytest.mark.parametrize(
    ("reason", "exit_code"),
    [("result", 0), ("blocked", 5), ("hung", 4), ("reasoning_stall", 8), ("timeout", 124)],
)
@respx.mock
def test_wait_exit_codes(state_root: Path, reason: str, exit_code: int) -> None:
    respx.get(f"{URL}/wait").respond(json={"reason": reason, "detail": "d", "status": STATUS})
    code, output = invoke(state_root, "wait", "--timeout", "5")
    assert code == exit_code
    assert output.startswith(f"{reason}: d")


@respx.mock
def test_allow_picks_the_only_pending_permission(state_root: Path) -> None:
    respx.get(f"{URL}/pending").respond(
        json={
            "permissions": [
                {"id": "per_7", "sessionID": "ses_1", "permission": "edit", "patterns": ["a"]}
            ],
            "questions": [],
        }
    )
    reply = respx.post(f"{URL}/permission").respond(json={"ok": True, "detail": "per_7 once"})

    code, output = invoke(state_root, "allow")

    assert code == 0
    assert output.strip() == "per_7 once"
    assert json.loads(reply.calls.last.request.content) == {"id": "per_7", "reply": "once"}


@respx.mock
def test_ambiguous_pending_needs_an_id(state_root: Path) -> None:
    respx.get(f"{URL}/pending").respond(json={"permissions": [], "questions": []})
    code, output = invoke(state_root, "deny")
    assert code == 2
    assert "0 pending permissions" in output


@respx.mock
def test_spec_reads_text_from_stdin(state_root: Path) -> None:
    route = respx.post(f"{URL}/spec").respond(
        json={"spec_id": "S-2", "session_id": "ses_2", "status": "active", "result_path": "/r"}
    )
    result = runner.invoke(
        app, ["--state-root", str(state_root), "spec", "S-2", "-"], input="Build it.\n"
    )
    assert result.exit_code == 0
    assert json.loads(route.calls.last.request.content) == {"spec_id": "S-2", "text": "Build it.\n"}


@respx.mock
def test_bridge_errors_are_one_line(state_root: Path) -> None:
    respx.post(f"{URL}/spec").respond(409, json={"detail": "Spec ID S-1 is immutable"})
    code, output = invoke(state_root, "spec", "S-1", "new", "text")
    assert code == 1
    assert output.strip() == "tandem: 409: Spec ID S-1 is immutable"


def test_no_supervisor_is_exit_3(tmp_path: Path) -> None:
    code, output = invoke(tmp_path / "empty", "status")
    assert code == 3
    assert "no running supervisor" in output


def test_several_supervisors_need_a_task(state_root: Path) -> None:
    write_manifest(state_root, "other-def456")
    code, output = invoke(state_root, "status")
    assert code == 2
    assert "pick one with --task" in output


@respx.mock
def test_task_prefix_selects_one(state_root: Path) -> None:
    write_manifest(state_root, "other-def456")
    respx.get(f"{URL}/status").respond(json=STATUS)
    code, _ = invoke(state_root, "--task", "demo", "status")
    assert code == 0


def test_dead_supervisors_are_skipped(tmp_path: Path) -> None:
    root = tmp_path / "state"
    write_manifest(root, "stale-000000", pid=2**22 + 12345)
    code, output = invoke(root, "status")
    assert code == 3
    assert "no running supervisor" in output


@respx.mock
def test_unreachable_bridge_is_exit_3(state_root: Path) -> None:
    import httpx

    respx.get(f"{URL}/status").mock(side_effect=httpx.ConnectError("refused"))
    code, output = invoke(state_root, "status")
    assert code == 3
    assert "not answering" in output


def test_serve_validates_settings_before_starting(tmp_path: Path) -> None:
    code, output = invoke(tmp_path, "serve", str(tmp_path / "missing"))
    assert code == 2
    assert "no such task directory" in output

    (tmp_path / "task").mkdir()
    code, output = invoke(tmp_path, "serve", str(tmp_path / "task"), "--model", "gpt-5")
    assert code == 2
    assert "provider/model" in output
