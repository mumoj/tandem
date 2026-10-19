"""The composition root: build every part, run them together, stop them together.

Everything runs in one process on one event loop, under one ``TaskGroup``:

* the bridge (uvicorn serving the FastAPI app),
* the event pump (OpenCode's stream into the supervisor), and
* the monitor (result file, protected files, stall checks on a timer).

If any of them dies unexpectedly the group cancels the rest, and the exit
stack below stops OpenCode and releases the lock on the way out. An earlier
plan put this inside FastAPI's lifespan. A task group wrapped around the
lifespan's ``yield`` cannot surface a background crash cleanly, so uvicorn runs
as one task of the group instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import signal
import socket
from collections.abc import Callable, Generator
from types import FrameType

import uvicorn
from pydantic import SecretStr

from tandem.bridge.app import OpenCodeLogin, create_app
from tandem.bridge.manifest import Manifest
from tandem.config import Settings, TaskPaths
from tandem.notify import Notifier
from tandem.opencode.client import OpenCodeClient, OpenCodeError
from tandem.opencode.events import StreamRecorder
from tandem.opencode.models import ModelRef
from tandem.opencode.server import (
    OpenCodeServer,
    Version,
    installed_version,
    require_supported,
    resolve_binary,
)
from tandem.supervisor.activity import ActivityTracker
from tandem.supervisor.journal import Journal
from tandem.supervisor.protected import ProtectedFiles
from tandem.supervisor.state import StateStore, SupervisorLock
from tandem.supervisor.supervisor import Supervisor

log = logging.getLogger(__name__)

# Events worth flushing the recorder for immediately; the rest are batched.
_FLUSH_ON = frozenset(
    {"permission.asked", "question.asked", "session.error", "session.idle", "server.connected"}
)


async def pump_events(
    client: OpenCodeClient,
    supervisor: Supervisor,
    recorder: StreamRecorder,
    *,
    max_delay: float = 30.0,
) -> None:
    """Feed the event stream to the supervisor, reconnecting with backoff, forever."""
    delay = 1.0
    while True:
        try:
            async for event in client.events():
                delay = 1.0
                recorder.write(event.raw, flush=event.type in _FLUSH_ON)
                try:
                    await supervisor.handle_event(event)
                except Exception:
                    # A bug in one handler must not stop the stream being read:
                    # everything downstream, including the next failure report,
                    # would wait behind it.
                    log.exception("event handler failed on %s", event.type)
            supervisor.journal.append("stream_closed")
        except OpenCodeError as exc:
            supervisor.journal.append("stream_failed", error=str(exc), retry_in=delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)


def bind_loopback(port: int) -> socket.socket:
    """Bind the bridge socket ourselves so its port is known before serving."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    return sock


@contextlib.contextmanager
def stop_on_signals(stop: asyncio.Event) -> Generator[None, None, None]:
    """Turn SIGINT/SIGTERM into ``stop.set()``.

    ``signal.signal`` rather than the loop's handler: uvicorn installs its own
    handlers while serving, restores the previous ones afterwards, and then
    re-raises the signal it caught. With these installed, that re-raise lands
    here and is harmless instead of interrupting the shutdown.
    """
    loop = asyncio.get_running_loop()

    def handler(_signum: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(stop.set)

    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, original in previous.items():
            signal.signal(sig, original)


async def _wait_until_serving(server: uvicorn.Server, serving: asyncio.Task[None]) -> None:
    while not server.started:
        if serving.done():
            serving.result()  # re-raise whatever stopped it
            raise RuntimeError("the bridge stopped before it started")
        await asyncio.sleep(0.05)


async def run(settings: Settings, *, ready: Callable[[Manifest], None] | None = None) -> None:
    """Supervise one task until ``/finish``, a signal, or a fatal error."""
    paths = TaskPaths.for_task(settings.state_root, settings.task_dir)
    paths.ensure()
    binary = resolve_binary(settings.opencode_bin)
    version = require_supported(await installed_version(binary))

    with SupervisorLock(paths.lock):
        async with contextlib.AsyncExitStack() as stack:
            server = await stack.enter_async_context(
                OpenCodeServer(
                    binary,
                    settings.task_dir,
                    paths.opencode_log,
                    settings.opencode_port,
                    protected=tuple(settings.protected),
                )
            )
            client = await stack.enter_async_context(
                OpenCodeClient(server.base_url, server.password)
            )
            # The binary's own report, in case PATH and OPENCODE_BIN disagree.
            require_supported(Version.parse((await client.health()).version))

            journal = Journal(paths.journal)
            notifier = Notifier(
                log_path=paths.notifications,
                task=settings.task_dir.name,
                state_dir=paths.root,
                command=settings.notify_cmd,
                ntfy_topic=settings.ntfy_topic,
                ntfy_server=settings.ntfy_server,
                timeout=settings.notify_timeout,
            )
            stack.push_async_callback(notifier.drain)
            supervisor = Supervisor(
                api=client,
                paths=paths,
                task_dir=settings.task_dir,
                store=StateStore(paths.state, settings.task_dir),
                journal=journal,
                notifier=notifier,
                tracker=ActivityTracker(
                    stall_after=settings.stall_after,
                    quiet_after=settings.quiet_after,
                    text_recent=settings.text_recent,
                ),
                protected=ProtectedFiles(settings.task_dir, tuple(settings.protected)),
                model=ModelRef.parse(settings.model) if settings.model else None,
            )
            await supervisor.start()
            stack.push_async_callback(supervisor.shutdown)
            recorder = stack.enter_context(StreamRecorder(paths.events, settings.max_stream_line))

            stop = asyncio.Event()
            token = secrets.token_urlsafe(32)
            app = create_app(
                supervisor=supervisor,
                token=token,
                on_finish=stop.set,
                opencode=OpenCodeLogin(server.base_url, server.password),
            )
            sock = stack.enter_context(bind_loopback(settings.bridge_port))
            bridge = uvicorn.Server(
                uvicorn.Config(
                    app,
                    log_level="warning",
                    access_log=False,
                    lifespan="off",
                    timeout_graceful_shutdown=5,
                )
            )
            manifest = Manifest(
                host="127.0.0.1",
                port=sock.getsockname()[1],
                token=SecretStr(token),
                task_dir=str(settings.task_dir),
                state_dir=str(paths.root),
                opencode_port=server.port,
                opencode_version=str(version),
                opencode_password=SecretStr(server.password),
            )

            with stop_on_signals(stop):
                try:
                    async with asyncio.TaskGroup() as group:
                        serving = group.create_task(bridge.serve(sockets=[sock]))
                        workers = [
                            group.create_task(pump_events(client, supervisor, recorder)),
                            group.create_task(supervisor.monitor(settings.poll_interval)),
                        ]
                        await _wait_until_serving(bridge, serving)
                        manifest.write(paths.manifest)
                        journal.append("bridge_ready", url=manifest.url, pid=manifest.pid)
                        if ready is not None:
                            ready(manifest)

                        stopping = group.create_task(stop.wait())
                        await asyncio.wait(
                            {serving, stopping, *workers}, return_when=asyncio.FIRST_COMPLETED
                        )
                        await supervisor.begin_shutdown()
                        bridge.should_exit = True
                        for task in (stopping, *workers):
                            task.cancel()
                finally:
                    Manifest.remove(paths.manifest)
