"""The OpenCode client's mapping onto HTTP, checked with respx."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from tandem.opencode.client import NotFoundError, OpenCodeClient, OpenCodeError
from tandem.opencode.models import ModelRef, PermissionAction, PermissionRule
from tandem.opencode.server import (
    UnsupportedVersionError,
    Version,
    permission_config,
    require_supported,
)

BASE = "http://127.0.0.1:4096"


@pytest.fixture
async def client() -> AsyncIterator[OpenCodeClient]:
    async with OpenCodeClient(BASE, "pw") as opencode:
        yield opencode


@respx.mock
async def test_requests_carry_basic_auth(client: OpenCodeClient) -> None:
    route = respx.get(f"{BASE}/global/health").respond(json={"healthy": True, "version": "1.19.3"})
    health = await client.health()
    assert health.version == "1.19.3"
    assert route.calls.last.request.headers["authorization"].startswith("Basic ")


@respx.mock
async def test_create_session_sends_the_ruleset_in_order(client: OpenCodeClient) -> None:
    route = respx.post(f"{BASE}/session").respond(json={"id": "ses_1", "title": "t"})
    rules = [
        PermissionRule(permission="edit", pattern="*", action=PermissionAction.ALLOW),
        PermissionRule(permission="edit", pattern="a.md", action=PermissionAction.ASK),
    ]
    session = await client.create_session("t", rules)

    assert session.id == "ses_1"
    body = json.loads(route.calls.last.request.content)
    assert body["permission"] == [
        {"permission": "edit", "pattern": "*", "action": "allow"},
        {"permission": "edit", "pattern": "a.md", "action": "ask"},
    ]


@respx.mock
async def test_prompt_can_name_an_agent(client: OpenCodeClient) -> None:
    route = respx.post(f"{BASE}/session/ses_1/prompt_async").respond(204)
    await client.prompt("ses_1", "hello", None, "plan")
    body = json.loads(route.calls.last.request.content)
    assert body == {"parts": [{"type": "text", "text": "hello"}], "agent": "plan"}


def test_protected_files_reach_every_agent_through_the_config() -> None:
    assert json.loads(permission_config(None, ("instruction.md",))) == {
        "edit": {"instruction.md": "ask"}
    }
    # The user's own settings survive, a plain action becomes the catch-all rule,
    # and protection wins over anything they said about the same file.
    mine = json.dumps({"bash": "ask", "edit": "allow"})
    assert json.loads(permission_config(mine, ("a.md",))) == {
        "bash": "ask",
        "edit": {"*": "allow", "a.md": "ask"},
    }
    mine = json.dumps({"edit": {"a.md": "allow", "b.md": "deny"}})
    assert json.loads(permission_config(mine, ("a.md",)))["edit"] == {"a.md": "ask", "b.md": "deny"}
    assert json.loads(permission_config("not json", ("a.md",))) == {"edit": {"a.md": "ask"}}


@respx.mock
async def test_prompt_is_async_and_names_the_model(client: OpenCodeClient) -> None:
    route = respx.post(f"{BASE}/session/ses_1/prompt_async").respond(204)
    await client.prompt("ses_1", "hello", ModelRef.parse("openrouter/qwen/qwen3-coder"))

    body = json.loads(route.calls.last.request.content)
    assert body == {
        "parts": [{"type": "text", "text": "hello"}],
        "model": {"providerID": "openrouter", "modelID": "qwen/qwen3-coder"},
    }


@respx.mock
async def test_permission_and_question_replies(client: OpenCodeClient) -> None:
    permission = respx.post(f"{BASE}/permission/per_1/reply").respond(json=True)
    reply = respx.post(f"{BASE}/question/que_1/reply").respond(json=True)
    reject = respx.post(f"{BASE}/question/que_2/reject").respond(json=True)

    assert await client.reply_permission("per_1", "reject", "not that file")
    assert await client.reply_question("que_1", [["Yes"]])
    assert await client.reject_question("que_2")

    assert json.loads(permission.calls.last.request.content) == {
        "reply": "reject",
        "message": "not that file",
    }
    assert json.loads(reply.calls.last.request.content) == {"answers": [["Yes"]]}
    assert reject.called


@respx.mock
async def test_lists_parse_into_models(client: OpenCodeClient) -> None:
    respx.get(f"{BASE}/permission").respond(
        json=[
            {
                "id": "per_1",
                "sessionID": "ses_1",
                "permission": "edit",
                "patterns": ["a.md"],
                "metadata": {"diff": "+x"},
                "always": ["*"],
            }
        ]
    )
    respx.get(f"{BASE}/session/status").respond(json={"ses_1": {"type": "busy"}})
    respx.get(f"{BASE}/session/ses_1/message").respond(
        json=[
            {
                "info": {"id": "msg_1", "role": "assistant", "cost": 0.01},
                "parts": [{"type": "text", "text": "done"}, {"type": "tool", "tool": "bash"}],
            }
        ]
    )

    [permission] = await client.permissions()
    assert (permission.session_id, permission.patterns) == ("ses_1", ["a.md"])
    assert (await client.session_status())["ses_1"].type == "busy"
    [message] = await client.messages("ses_1")
    assert message.text() == "done"


@respx.mock
async def test_errors_carry_status_and_detail(client: OpenCodeClient) -> None:
    respx.get(f"{BASE}/session/ses_x").respond(404, json={"name": "NotFoundError"})
    respx.post(f"{BASE}/session/ses_1/abort").respond(500, text="kaboom")
    respx.get(f"{BASE}/permission").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(NotFoundError):
        await client.get_session("ses_x")
    with pytest.raises(OpenCodeError, match="500: kaboom"):
        await client.abort("ses_1")
    with pytest.raises(OpenCodeError, match="ConnectError") as caught:
        await client.permissions()
    assert caught.value.status_code is None


@respx.mock
async def test_event_stream_yields_parsed_events(client: OpenCodeClient) -> None:
    payload = (
        'data: {"id":"evt_1","type":"server.connected","properties":{}}\n\n'
        "data: not-json\n\n"
        'data: {"id":"evt_2","type":"session.idle","properties":{"sessionID":"ses_1"}}\n\n'
    )
    respx.get(f"{BASE}/event").respond(
        headers={"content-type": "text/event-stream"}, content=payload.encode()
    )
    events = [event async for event in client.events()]
    assert [e.type for e in events] == ["server.connected", "session.idle"]
    assert events[1].session_id == "ses_1"
    assert events[1].raw.startswith('{"id":"evt_2"')


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1.19.3", Version(1, 19, 3)), ("opencode v2.0.6\n", Version(2, 0, 6))],
)
def test_version_parsing(text: str, expected: Version) -> None:
    assert Version.parse(text) == expected


def test_opencode_2_is_refused_with_a_way_out() -> None:
    assert require_supported(Version(1, 19, 3)) == Version(1, 19, 3)
    with pytest.raises(UnsupportedVersionError, match="opencode-ai@1"):
        require_supported(Version(2, 0, 6))


def test_model_ref_needs_a_provider() -> None:
    ref = ModelRef.parse("anthropic/claude-sonnet-4-5")
    assert (ref.provider_id, ref.model_id) == ("anthropic", "claude-sonnet-4-5")
    with pytest.raises(ValueError, match="provider/model"):
        ModelRef.parse("gpt-5")
