"""The bridge over a real supervisor and a fake OpenCode, in process via ASGI."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from tandem.bridge.app import create_app
from tandem.supervisor.supervisor import Supervisor
from tests.fakes import FakeOpenCode

TOKEN = "t0ken"


@pytest.fixture
async def http(supervisor: Supervisor) -> AsyncIterator[httpx.AsyncClient]:
    finished: list[bool] = []
    app = create_app(supervisor=supervisor, token=TOKEN, on_finish=lambda: finished.append(True))
    app.state.finished = finished
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://bridge",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        yield client


async def test_every_route_needs_the_token(http: httpx.AsyncClient) -> None:
    del http.headers["Authorization"]
    assert (await http.get("/status")).status_code == 401
    http.headers["Authorization"] = "Bearer wrong"
    assert (await http.post("/abort")).status_code == 401


async def test_spec_lifecycle(http: httpx.AsyncClient, api: FakeOpenCode) -> None:
    created = await http.post("/spec", json={"spec_id": "S-1", "text": "Add a greeting."})
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "active"
    assert body["result_path"].endswith(".json")

    status = (await http.get("/status")).json()
    assert status["spec_id"] == "S-1"
    assert status["activity"]["activity"] == "working"

    changed = await http.post("/spec", json={"spec_id": "S-1", "text": "Something else."})
    assert changed.status_code == 409
    assert "immutable" in changed.json()["detail"]

    said = await http.post("/prompt", json={"text": "also add tests"})
    assert said.status_code == 202
    assert api.prompts(body["session_id"])[-1] == "also add tests"

    messages = (await http.get("/messages", params={"limit": 1})).json()
    assert messages["messages"] == [{"role": "user", "text": "also add tests", "tools": []}]


async def test_ids_are_validated_before_any_handler(http: httpx.AsyncClient) -> None:
    bad_spec = await http.post("/spec", json={"spec_id": "../etc", "text": "x"})
    assert bad_spec.status_code == 422
    blank = await http.post("/spec", json={"spec_id": "S-1", "text": "   "})
    assert blank.status_code == 422
    bad_permission = await http.post("/permission", json={"id": "per_1/../x", "reply": "once"})
    assert bad_permission.status_code == 422
    bad_reply = await http.post("/permission", json={"id": "per_1", "reply": "sometimes"})
    assert bad_reply.status_code == 422
    both = await http.post("/answer", json={"id": "que_1", "answers": [["a"]], "reject": True})
    assert both.status_code == 422


async def test_directives_through_the_bridge(http: httpx.AsyncClient) -> None:
    await http.post("/spec", json={"spec_id": "S-1", "text": "Add a greeting."})
    sent = await http.post(
        "/directive",
        json={"spec_id": "S-1", "directive_id": "D-1", "action": "CORRECT", "text": "French."},
    )
    assert sent.json() == {
        "spec_id": "S-1",
        "directive_id": "D-1",
        "action": "CORRECT",
        "retracted": False,
    }
    stale = await http.post(
        "/directive",
        json={"spec_id": "S-0", "directive_id": "D-2", "action": "CORRECT", "text": "x"},
    )
    assert stale.status_code == 409
    retracted = await http.post("/retract", json={"spec_id": "S-1", "directive_id": "D-1"})
    assert retracted.json()["retracted"] is True


async def test_releasing_a_blocked_session(http: httpx.AsyncClient, api: FakeOpenCode) -> None:
    body = (await http.post("/spec", json={"spec_id": "S-1", "text": "x"})).json()
    request_id = api.ask_permission(body["session_id"])

    pending = (await http.get("/pending")).json()
    assert [p["id"] for p in pending["permissions"]] == [request_id]

    reply = await http.post("/permission", json={"id": request_id, "reply": "once"})
    assert reply.json() == {"ok": True, "detail": f"{request_id} once"}

    gone = await http.post("/permission", json={"id": request_id, "reply": "once"})
    assert gone.status_code == 404


async def test_wait_returns_the_reason(http: httpx.AsyncClient) -> None:
    await http.post("/spec", json={"spec_id": "S-1", "text": "x"})
    waited = (await http.get("/wait", params={"timeout": 0.05})).json()
    assert waited["reason"] == "timeout"
    assert waited["status"]["spec_id"] == "S-1"


async def test_reads_without_a_spec_are_conflicts(http: httpx.AsyncClient) -> None:
    for path in ("/messages", "/diff"):
        response = await http.get(path)
        assert response.status_code == 409
        assert "no active spec" in response.json()["detail"]
    assert (await http.get("/result")).json() == {"path": None, "result": None}


async def test_cost_diff_and_finish(http: httpx.AsyncClient) -> None:
    await http.post("/spec", json={"spec_id": "S-1", "text": "x"})
    cost = (await http.get("/cost")).json()
    assert cost["specs"][0]["spec_id"] == "S-1"
    diff = (await http.get("/diff")).json()
    assert diff["files"][0]["file"] == "hello.txt"

    finished = await http.post("/finish")
    assert finished.json()["detail"] == "stopping"


async def test_oversized_bodies_are_refused(http: httpx.AsyncClient) -> None:
    response = await http.post("/prompt", content=b"x" * 70_000)
    assert response.status_code == 413
