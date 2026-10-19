"""One ``opencode serve`` shared by every implementer of a planner session.

Starting OpenCode costs seconds and hundreds of megabytes, so ``tandem mcp``
starts one when the planner connects and every supervisor it spawns attaches to
it, each scoped to its own directory. The host lives exactly as long as the MCP
connection: it is stopped when the planner disconnects, and on Linux the kernel
stops it too should the MCP server die without cleaning up.

This side is synchronous on purpose. It is driven from the MCP server's worker
threads and its lifespan, and a plain process with a readiness flag is simpler
to reason about there than an event loop of its own.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from tandem.opencode.server import Version, free_port, require_supported
from tandem.supervisor.state import write_private

_PR_SET_PDEATHSIG = 1


class HostError(RuntimeError):
    """The shared server failed to start, or stopped."""


def _die_with_parent() -> None:  # pragma: no cover - runs in the child
    """Have the kernel send SIGTERM if the process that started us dies."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)


@dataclass
class SharedHost:
    """An ``opencode serve`` on loopback for several directories at once.

    ``start`` returns at once; ``wait_ready`` blocks until it answers. A record
    of the host, written 0600 under ``records``, lets a later session find and
    stop a host whose owner died without stopping it.
    """

    binary: str
    log_path: Path
    records: Path
    startup_timeout: float = 60.0
    port: int = field(default_factory=free_port)
    password: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    process: subprocess.Popen[bytes] | None = field(default=None, init=False)
    version: Version | None = field(default=None, init=False)
    _ready: threading.Event = field(default_factory=threading.Event, init=False)
    _failure: str | None = field(default=None, init=False)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def record(self) -> Path:
        return self.records / f"{os.getpid()}.json"

    def start(self, *, die_with_parent: bool = True) -> None:
        """Launch the server and return at once.

        ``die_with_parent`` asks the kernel to stop the server with the thread
        that starts it, which is only right from a thread that lives as long as
        the owning process: its main thread.
        """
        self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {**os.environ, "OPENCODE_SERVER_PASSWORD": self.password}
        argv = [self.binary, "serve", "--hostname", "127.0.0.1", "--port", str(self.port)]
        on_fork = _die_with_parent if die_with_parent and sys.platform == "linux" else None
        with self.log_path.open("ab") as log:
            self.process = subprocess.Popen(
                argv,
                cwd=self.log_path.parent,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                # Runs between fork and exec, where only async-signal-safe calls
                # belong. prctl is one: it takes no locks the parent might hold.
                preexec_fn=on_fork,
            )
        write_private(
            self.record,
            json.dumps({"owner": os.getpid(), "pid": self.process.pid, "url": self.url}) + "\n",
        )
        threading.Thread(target=self._await_health, name="opencode-host", daemon=True).start()

    def _await_health(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        with httpx.Client(auth=("opencode", self.password), timeout=2.0) as http:
            while time.monotonic() < deadline:
                if self.process is not None and self.process.poll() is not None:
                    self._failure = f"opencode serve exited with {self.process.returncode}"
                    break
                with contextlib.suppress(httpx.HTTPError):
                    response = http.get(f"{self.url}/global/health")
                    if response.is_success:
                        try:
                            self.version = require_supported(
                                Version.parse(response.json()["version"])
                            )
                        except (RuntimeError, KeyError, ValueError) as exc:
                            self._failure = str(exc)
                        break
                time.sleep(0.25)
            else:
                self._failure = f"opencode serve did not answer within {self.startup_timeout:.0f}s"
        self._ready.set()

    def wait_ready(self, timeout: float) -> None:
        """Block until the host answers, or raise why it will not."""
        if not self._ready.wait(timeout):
            raise HostError(f"the shared OpenCode server is still starting; see {self.log_path}")
        if self._failure is not None:
            raise HostError(
                f"the shared OpenCode server failed: {self._failure}; see {self.log_path}"
            )

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self, timeout: float = 10.0) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            # Dispose first so OpenCode flushes every instance's state, then signal.
            with contextlib.suppress(httpx.HTTPError):
                httpx.post(
                    f"{self.url}/global/dispose",
                    auth=("opencode", self.password),
                    timeout=timeout / 2,
                )
            process.terminate()
            try:
                process.wait(timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        with contextlib.suppress(FileNotFoundError):
            self.record.unlink()


def reap_orphans(records: Path) -> list[int]:
    """Stop hosts whose owner died without stopping them. Returns their pids.

    The kernel does this on Linux; elsewhere, or for a host from before that,
    the next session tidies up.
    """
    reaped: list[int] = []
    for path in records.glob("*.json"):
        try:
            record = json.loads(path.read_text())
            owner, pid = int(record["owner"]), int(record["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            continue
        if _alive(owner):
            continue
        if _alive(pid) and _is_opencode(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
                reaped.append(pid)
        path.unlink(missing_ok=True)
    return reaped


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_opencode(pid: int) -> bool:
    """Guard against a recycled pid: only ever signal an ``opencode serve``."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return sys.platform != "linux"  # no /proc to check: trust the record
    return any(part.endswith(b"opencode") for part in cmdline) and b"serve" in cmdline
