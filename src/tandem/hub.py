"""Every task of one planner session, behind one server.

``tandem serve`` runs a task behind a bridge of its own. When the MCP server
drives several implementers, that would be a uvicorn and an OpenCode per
implementer. The hub runs them all in this process instead: one FastAPI app
with each task's bridge mounted at ``/t/<task>``, one uvicorn, and one shared
OpenCode server.

A task's manifest points at its mount, so the CLI, the MCP tools and the
dashboard address a hub task exactly as they address a standalone one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any

import anyio.to_thread
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

from tandem.bridge.app import dashboard_headers, require_token
from tandem.bridge.manifest import Manifest
from tandem.config import Settings, TaskPaths
from tandem.opencode.host import SharedHost
from tandem.runtime import bind_loopback, open_task

log = logging.getLogger(__name__)


class _Embedded(uvicorn.Server):
    """uvicorn without signal handlers of its own: the process is the MCP server's."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class TaskRoutes:
    """The ASGI app at ``/t/{key}``: hands each request to that task's bridge.

    Mounted once. Tasks come and go in a dict, so the router itself never
    changes while it is serving.
    """

    def __init__(self) -> None:
        self.apps: dict[str, ASGIApp] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        app = self.apps.get(scope.get("path_params", {}).get("key", ""))
        if app is None:
            response = JSONResponse({"detail": "no such task, or it has finished"}, 404)
            await response(scope, receive, send)
            return
        await app(scope, receive, send)


class HubTask(BaseModel):
    name: str
    task_dir: str
    dashboard: str


class HubTasks(BaseModel):
    tasks: list[HubTask]


@dataclass
class _Running:
    key: str
    stop: asyncio.Event
    done: asyncio.Task[None]


@dataclass
class Hub:
    """One FastAPI server and one OpenCode server for many supervised tasks."""

    state_root: Path
    new_opencode: Callable[[], SharedHost]
    """Starts the shared OpenCode server. Called on the event loop's thread."""
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    port: int = field(default=0, init=False)
    app: FastAPI = field(init=False)
    opencode: SharedHost | None = field(default=None, init=False)
    _server: _Embedded | None = field(default=None, init=False)
    _serving: asyncio.Task[None] | None = field(default=None, init=False)
    _tasks: dict[Path, _Running] = field(default_factory=dict[Path, _Running], init=False)
    routes: TaskRoutes = field(default_factory=TaskRoutes, init=False)

    def __post_init__(self) -> None:
        self.app = FastAPI(title="tandem hub", docs_url=None, redoc_url=None, openapi_url=None)
        self.app.state.token = self.token
        self.app.state.hub = self
        self.app.include_router(router)
        self.app.mount("/t/{key}", self.routes)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def dashboard_url(self) -> str:
        return f"{self.url}/ui#token={self.token}"

    async def start(self) -> None:
        """Start the shared OpenCode server in the background, then serve at once.

        OpenCode takes seconds to start; the first task waits for it, so the
        planner's connection is not held up.
        """
        self.opencode = self.new_opencode()
        sock = bind_loopback(0)
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(self.app, log_level="warning", access_log=False, lifespan="off")
        self._server = _Embedded(config)
        self._serving = asyncio.create_task(self._server.serve(sockets=[sock]))
        while not self._server.started:
            if self._serving.done():
                self._serving.result()  # re-raise whatever stopped it
                raise RuntimeError("the hub stopped before it started")
            await asyncio.sleep(0.02)

    async def close(self) -> None:
        """Stop every task, capturing its evidence, then the server."""
        await asyncio.gather(*(self.stop_task(task_dir) for task_dir in list(self._tasks)))
        if self._server is not None and self._serving is not None:
            self._server.should_exit = True
            await self._serving
        if self.opencode is not None:
            await anyio.to_thread.run_sync(self.opencode.stop)

    def running(self, task_dir: Path) -> bool:
        return task_dir in self._tasks

    async def start_task(
        self, task_dir: Path, *, model: str | None = None, protect: list[str] | None = None
    ) -> Manifest:
        """Supervise ``task_dir``, mounted on this server. Idempotent."""
        paths = TaskPaths.for_task(self.state_root, task_dir)
        if task_dir in self._tasks:
            return Manifest.read(paths.manifest)
        opencode = await self._ready_opencode()
        # Only what was asked for: an explicit None would hide TANDEM_MODEL and
        # TANDEM_PROTECTED, since pydantic-settings prefers arguments to the environment.
        chosen: dict[str, Any] = {"model": model, "protected": protect}
        settings = Settings(
            task_dir=task_dir,
            state_root=self.state_root,
            opencode_url=opencode.url,
            opencode_password=SecretStr(opencode.password),
            **{key: value for key, value in chosen.items() if value is not None},
        )
        ready: asyncio.Future[Manifest] = asyncio.get_running_loop().create_future()
        done = asyncio.create_task(self._run(settings, paths, ready))
        try:
            return await ready
        except BaseException:
            done.cancel()
            raise

    async def _ready_opencode(self) -> SharedHost:
        """The shared server, started again if it died; its tasks stopped with it."""
        if self.opencode is None or (self.opencode.process and not self.opencode.alive):
            self.opencode = self.new_opencode()
        await anyio.to_thread.run_sync(self.opencode.wait_ready, self.opencode.startup_timeout)
        return self.opencode

    async def stop_task(self, task_dir: Path) -> bool:
        """Stop one task as ``/finish`` would. False if it was not running."""
        running = self._tasks.get(task_dir)
        if running is None:
            return False
        running.stop.set()
        with contextlib.suppress(Exception):
            await running.done
        return True

    def list_tasks(self) -> HubTasks:
        return HubTasks(
            tasks=[
                HubTask(
                    name=task_dir.name,
                    task_dir=str(task_dir),
                    dashboard=f"{self.url}/t/{running.key}/ui#token={self.token}",
                )
                for task_dir, running in sorted(self._tasks.items())
            ]
        )

    async def _run(
        self, settings: Settings, paths: TaskPaths, ready: asyncio.Future[Manifest]
    ) -> None:
        key = paths.root.name
        current = asyncio.current_task()
        assert current is not None
        try:
            async with open_task(settings, token=self.token) as task:
                self.routes.apps[key] = task.app
                self._tasks[settings.task_dir] = _Running(key, task.stop, current)
                try:
                    manifest = task.manifest(
                        host="127.0.0.1", port=self.port, token=self.token, path=f"/t/{key}"
                    )
                    manifest.write(paths.manifest)
                    task.journal.append("bridge_ready", url=manifest.url, hub=self.url)
                    ready.set_result(manifest)
                    await task.run_workers()
                finally:
                    self.routes.apps.pop(key, None)
                    self._tasks.pop(settings.task_dir, None)
                    Manifest.remove(paths.manifest)
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                # One task failing must not take the others, or the hub, with it.
                log.exception("task %s stopped with an error", key)


def get_hub(request: Request) -> Hub:
    hub: Hub = request.app.state.hub
    return hub


router = APIRouter()


@router.get("/tasks", dependencies=[Depends(require_token)])
def read_tasks(hub: Annotated[Hub, Depends(get_hub)]) -> HubTasks:
    return hub.list_tasks()


@router.get("/ui", include_in_schema=False)
def overview_page() -> HTMLResponse:
    """Every task of the session. Public, holds no data: see ``tandem.bridge.app``."""
    page = files("tandem.bridge").joinpath("hub.html").read_text(encoding="utf-8")
    return HTMLResponse(page, headers=dashboard_headers())
