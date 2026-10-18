"""The manifest: how a client finds a running bridge.

It carries the bearer token, so it is written 0600 before any content exists
and removed when the supervisor stops cleanly.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_serializer

from tandem.supervisor.state import write_private


class Manifest(BaseModel):
    version: Literal[1] = 1
    host: str
    port: int
    token: SecretStr
    task_dir: str
    state_dir: str
    pid: int = Field(default_factory=os.getpid)
    opencode_port: int | None = None
    opencode_version: str | None = None
    started_at: float = Field(default_factory=time.time)

    @field_serializer("token", when_used="json")
    def _reveal_token(self, token: SecretStr) -> str:
        # The manifest file is the one place the token is meant to be written.
        return token.get_secret_value()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def alive(self) -> bool:
        """Whether the process that wrote this manifest still exists."""
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def write(self, path: Path) -> None:
        write_private(path, self.model_dump_json(indent=2) + "\n")

    @classmethod
    def read(cls, path: Path) -> Manifest:
        return cls.model_validate_json(path.read_bytes())

    @staticmethod
    def remove(path: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
