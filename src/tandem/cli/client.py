"""Find a running bridge and talk to it.

Responses are parsed into the same pydantic models the bridge serves, so the
client and server cannot drift apart without a validation error saying where.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from tandem.bridge.manifest import Manifest
from tandem.config import TaskPaths

NO_BRIDGE = 3


class CliError(Exception):
    """A failure to report in one line, with the exit code to report it with."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def find_manifest(state_root: Path, *, task: str | None, manifest: Path | None) -> Path:
    """Resolve which supervisor a command is for.

    In order: an explicit manifest; ``--task`` as a task directory, a state
    directory, or a name prefix; otherwise the only running supervisor. That
    last rule is what keeps a phone session down to ``tandem status``.
    """
    if manifest is not None:
        if not manifest.is_file():
            raise CliError(f"no such manifest: {manifest}", NO_BRIDGE)
        return manifest

    candidates = sorted(state_root.glob("*/manifest.json"))
    if task:
        path = Path(task).expanduser()
        if (path / "manifest.json").is_file():
            return path / "manifest.json"
        if path.is_dir():
            owned = TaskPaths.for_task(state_root, path.resolve()).manifest
            if owned.is_file():
                return owned
            raise CliError(f"no running supervisor for {path}", NO_BRIDGE)
        candidates = [c for c in candidates if c.parent.name.startswith(task)]

    live = [c for c in candidates if _alive(c)]
    if len(live) == 1:
        return live[0]
    if not live:
        hint = f" matching {task!r}" if task else ""
        raise CliError(f"no running supervisor{hint} (start one with `tandem serve`)", NO_BRIDGE)
    names = ", ".join(c.parent.name for c in live)
    raise CliError(f"{len(live)} supervisors running ({names}); pick one with --task", 2)


def _alive(path: Path) -> bool:
    try:
        return Manifest.read(path).alive
    except (OSError, ValueError):
        return False


class BridgeClient:
    def __init__(
        self,
        manifest: Manifest,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.manifest = manifest
        self._http = httpx.Client(
            base_url=manifest.url,
            headers={"Authorization": f"Bearer {manifest.token.get_secret_value()}"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def get[T: BaseModel](
        self,
        path: str,
        model: type[T],
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> T:
        return self._send("GET", path, model, params=params, timeout=timeout)

    def post[T: BaseModel](
        self, path: str, model: type[T], body: dict[str, Any] | None = None
    ) -> T:
        return self._send("POST", path, model, json=body)

    def _send[T: BaseModel](
        self,
        method: str,
        path: str,
        model: type[T],
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> T:
        try:
            response = self._http.request(
                method,
                path,
                json=json,
                params=params,
                timeout=timeout if timeout is not None else self._http.timeout,
            )
        except httpx.TransportError as exc:
            raise CliError(
                f"bridge at {self.manifest.url} is not answering ({type(exc).__name__}); "
                "is `tandem serve` still running?",
                NO_BRIDGE,
            ) from exc
        if response.is_error:
            raise CliError(f"{response.status_code}: {_detail(response)}")
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            raise CliError(f"unexpected response from {path}: {exc}") from exc


class _ErrorBody(BaseModel):
    detail: str | list[dict[str, Any]] = ""


def _detail(response: httpx.Response) -> str:
    try:
        body = _ErrorBody.model_validate_json(response.content)
    except ValidationError:
        return response.text[:300]
    if isinstance(body.detail, str):
        return body.detail
    # FastAPI validation errors: name the field and the problem.
    return "; ".join(
        f"{'.'.join(str(part) for part in item.get('loc', [])[1:])}: {item.get('msg')}"
        for item in body.detail
    )
