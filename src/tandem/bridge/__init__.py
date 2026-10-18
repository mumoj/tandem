"""The authenticated control API over the supervisor."""

from tandem.bridge.app import create_app
from tandem.bridge.manifest import Manifest

__all__ = ["Manifest", "create_app"]
