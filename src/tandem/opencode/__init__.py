"""Everything that talks to OpenCode: models, client, event stream, process."""

from tandem.opencode.client import NotFoundError, OpenCodeAPI, OpenCodeClient, OpenCodeError
from tandem.opencode.events import Event, StreamRecorder
from tandem.opencode.models import ModelRef, PermissionAction, PermissionRule
from tandem.opencode.server import OpenCodeServer, UnsupportedVersionError, Version

__all__ = [
    "Event",
    "ModelRef",
    "NotFoundError",
    "OpenCodeAPI",
    "OpenCodeClient",
    "OpenCodeError",
    "OpenCodeServer",
    "PermissionAction",
    "PermissionRule",
    "StreamRecorder",
    "UnsupportedVersionError",
    "Version",
]
