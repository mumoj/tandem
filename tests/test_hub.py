"""The hub against a real ``opencode serve``: many tasks, one server each of ours and theirs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from tandem.bridge.manifest import Manifest
from tandem.config import TaskPaths
from tandem.hub import Hub
from tandem.opencode.host import SharedHost
from tests.test_e2e import OPENCODE, requires_opencode

pytestmark = [pytest.mark.e2e, requires_opencode]


@pytest.fixture
async def hub(tmp_path: Path) -> AsyncIterator[Hub]:
    assert OPENCODE is not None
    binary = OPENCODE
    records = tmp_path / "state" / "hosts"

    def new_opencode() -> SharedHost:
        host = SharedHost(binary, log_path=records / "host.log", records=records)
        host.start(die_with_parent=False)
        return host

    hub = Hub(tmp_path / "state", new_opencode)
    await hub.start()
    try:
        yield hub
    finally:
        await hub.close()


def task_dir(tmp_path: Path, name: str) -> Path:
    directory = (tmp_path / name).resolve()
    directory.mkdir()
    (directory / "instruction.md").write_text("Never edit this.\n")
    return directory


def bridge(manifest: Manifest) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=manifest.url,
        headers={"Authorization": f"Bearer {manifest.token.get_secret_value()}"},
    )


async def test_two_tasks_share_one_server_and_one_opencode(hub: Hub, tmp_path: Path) -> None:
    alpha = await hub.start_task(task_dir(tmp_path, "alpha"), protect=["instruction.md"])
    beta = await hub.start_task(task_dir(tmp_path, "beta"))

    assert hub.opencode is not None
    assert alpha.port == beta.port == hub.port
    assert alpha.opencode_port == beta.opencode_port == hub.opencode.port
    assert alpha.path != beta.path
    for manifest in (alpha, beta):
        async with bridge(manifest) as client:
            status = (await client.get("/status")).json()
            assert status["task_dir"] == manifest.task_dir
            assert (await client.get("/ui")).status_code == 200

    async with httpx.AsyncClient(base_url=hub.url) as client:
        listing = await client.get("/tasks", headers={"Authorization": f"Bearer {hub.token}"})
        assert sorted(t["name"] for t in listing.json()["tasks"]) == ["alpha", "beta"]
        assert (await client.get("/tasks")).status_code == 401


async def test_starting_a_task_twice_is_a_no_op(hub: Hub, tmp_path: Path) -> None:
    directory = task_dir(tmp_path, "alpha")
    first = await hub.start_task(directory)
    again = await hub.start_task(directory)
    assert again.path == first.path


async def test_finishing_a_task_unmounts_it_and_leaves_the_others(hub: Hub, tmp_path: Path) -> None:
    alpha_dir = task_dir(tmp_path, "alpha")
    alpha = await hub.start_task(alpha_dir)
    beta = await hub.start_task(task_dir(tmp_path, "beta"))

    async with bridge(alpha) as client:
        assert (await client.post("/finish")).json()["detail"] == "stopping"
    for _ in range(200):
        if not hub.running(alpha_dir):
            break
        await asyncio.sleep(0.05)
    assert not hub.running(alpha_dir)
    assert not TaskPaths.for_task(hub.state_root, alpha_dir).manifest.exists()
    async with bridge(alpha) as client:
        assert (await client.get("/status")).status_code == 404
    async with bridge(beta) as client:
        assert (await client.get("/status")).status_code == 200


async def test_closing_the_hub_stops_its_tasks_and_opencode(tmp_path: Path) -> None:
    assert OPENCODE is not None
    binary = OPENCODE
    records = tmp_path / "state" / "hosts"
    started: list[SharedHost] = []

    def new_opencode() -> SharedHost:
        host = SharedHost(binary, log_path=records / "host.log", records=records)
        host.start(die_with_parent=False)
        started.append(host)
        return host

    hub = Hub(tmp_path / "state", new_opencode)
    await hub.start()
    directory = task_dir(tmp_path, "alpha")
    await hub.start_task(directory)
    await hub.close()

    [host] = started
    assert not host.alive
    assert not host.record.exists()
    assert not TaskPaths.for_task(hub.state_root, directory).manifest.exists()
