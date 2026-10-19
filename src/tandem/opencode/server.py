"""Run ``opencode serve`` as a child process and check it is a version we speak.

OpenCode 2 replaced the server API this package targets, and its installer
replaces the 1.x binary in place. One upgrade on the host would otherwise turn
every call into a 404 with no hint why, so the version is checked before the
server starts and again from its own health report.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast

import httpx

log = logging.getLogger(__name__)

SUPPORTED_MAJOR = 1
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class UnsupportedVersionError(RuntimeError):
    """The installed OpenCode speaks a server API this package does not."""


@dataclass(frozen=True, order=True, slots=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> Version:
        match = _VERSION.search(text)
        if match is None:
            raise UnsupportedVersionError(f"cannot read an OpenCode version from {text.strip()!r}")
        major, minor, patch = (int(group) for group in match.groups())
        return cls(major, minor, patch)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def require_supported(version: Version) -> Version:
    if version.major != SUPPORTED_MAJOR:
        raise UnsupportedVersionError(
            f"OpenCode {version} is not supported: tandem speaks the OpenCode "
            f"{SUPPORTED_MAJOR}.x server API, which OpenCode 2 replaced. "
            "Install the 1.x line with `npm i -g opencode-ai@1`."
        )
    return version


def resolve_binary(name: str) -> str:
    """An absolute path to the OpenCode binary, or a clear error."""
    found = shutil.which(name)
    if found:
        return found
    fallback = Path.home() / ".opencode" / "bin" / "opencode"
    if name == "opencode" and fallback.is_file():
        return str(fallback)
    raise FileNotFoundError(f"OpenCode binary {name!r} not found on PATH")


async def installed_version(binary: str) -> Version:
    process = await asyncio.create_subprocess_exec(
        binary,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    return Version.parse(output.decode(errors="replace"))


def free_port() -> int:
    """A port that was free a moment ago.

    ``opencode serve --port 0`` does not bind an ephemeral port; it falls back
    to its default 4096. So the port is chosen here, both to avoid colliding
    with another server and so it is known before the server starts.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def permission_config(existing: str | None, protected: tuple[str, ...]) -> str:
    """``OPENCODE_PERMISSION`` with an ask rule for each protected glob merged in.

    The per-session ruleset reaches only the session tandem creates. A subagent
    started by the task tool gets a session of its own with its agent's rules,
    so without this it could edit a protected file unasked. Config-level rules
    land after each agent's broad allow, and OpenCode lets the last match win.
    """
    config: dict[str, Any] = {}
    if existing:
        with contextlib.suppress(ValueError):
            parsed = json.loads(existing)
            if isinstance(parsed, dict):
                config = cast(dict[str, Any], parsed)
    edit = config.get("edit")
    rules: dict[str, Any] = (
        dict(cast(dict[str, Any], edit)) if isinstance(edit, dict) else {"*": edit} if edit else {}
    )
    rules |= dict.fromkeys(protected, "ask")
    if rules:
        config["edit"] = rules
    return json.dumps(config)


@dataclass
class OpenCodeServer:
    """One ``opencode serve`` on loopback, protected by a per-run password.

    Use as an async context manager: entering starts the process and waits for
    it to answer; leaving disposes the instance and stops it.
    """

    binary: str
    task_dir: Path
    log_path: Path
    port: int = 0
    startup_timeout: float = 30.0
    protected: tuple[str, ...] = ()
    """Globs every session must ask before editing, subagents' included."""
    password: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    process: asyncio.subprocess.Process | None = field(default=None, init=False)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        if not self.port:
            self.port = free_port()
        env = {**os.environ, "OPENCODE_SERVER_PASSWORD": self.password}
        if self.protected:
            env["OPENCODE_PERMISSION"] = permission_config(
                os.environ.get("OPENCODE_PERMISSION"), self.protected
            )
        # Append, never truncate: after a crash the previous incarnation's
        # output is the only record of why it stopped.
        with self.log_path.open("ab") as sink:
            self.process = await asyncio.create_subprocess_exec(
                self.binary,
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(self.port),
                cwd=self.task_dir,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=sink,
                stderr=sink,
            )
        await self._wait_until_healthy()
        log.info("opencode serve pid=%s port=%s", self.process.pid, self.port)

    async def _wait_until_healthy(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.startup_timeout
        async with httpx.AsyncClient(auth=("opencode", self.password), timeout=2.0) as http:
            while loop.time() < deadline:
                if self.process is not None and self.process.returncode is not None:
                    raise RuntimeError(
                        f"opencode serve exited with {self.process.returncode}; see {self.log_path}"
                    )
                with contextlib.suppress(httpx.HTTPError):
                    response = await http.get(f"{self.base_url}/global/health")
                    if response.is_success:
                        return
                await asyncio.sleep(0.25)
        await self.stop()
        raise RuntimeError(f"opencode serve did not answer on {self.port}; see {self.log_path}")

    async def stop(self) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            return
        # Dispose first so OpenCode flushes its own state, then signal.
        with contextlib.suppress(httpx.HTTPError):
            async with httpx.AsyncClient(auth=("opencode", self.password), timeout=5.0) as http:
                await http.post(f"{self.base_url}/instance/dispose")
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
