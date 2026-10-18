"""Files the implementer must not change, enforced twice.

"Do not edit instruction.md" started life as a sentence in a prompt, and a
sentence is a request. Here it becomes two mechanisms:

* a permission rule, so an attempted edit stops and waits for a human with the
  diff attached instead of simply happening; and
* a content hash, checked while the spec runs, which catches the edits that
  arrive some other way (a shell redirect, a script the model wrote).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from tandem.opencode.models import PermissionAction, PermissionRule

MISSING = "<missing>"

# What an unattended run needs to avoid stalling on OpenCode's cautious
# defaults. Broad rules come first: OpenCode lets the last matching rule win.
UNATTENDED_RULES: tuple[PermissionRule, ...] = (
    PermissionRule(permission="edit", pattern="*", action=PermissionAction.ALLOW),
    PermissionRule(permission="external_directory", pattern="*", action=PermissionAction.ALLOW),
    PermissionRule(permission="doom_loop", pattern="*", action=PermissionAction.ALLOW),
)


@dataclass(frozen=True, slots=True)
class ProtectedFiles:
    root: Path
    patterns: tuple[str, ...] = ()

    def rules(self) -> list[PermissionRule]:
        """The session ruleset: unattended defaults, then one ask per pattern."""
        asks = [
            PermissionRule(permission="edit", pattern=pattern, action=PermissionAction.ASK)
            for pattern in self.patterns
        ]
        return [*UNATTENDED_RULES, *asks]

    def snapshot(self) -> dict[str, str]:
        """A hash per matching file, plus a marker for patterns that match nothing.

        Recording the empty patterns means creating a protected file counts as a
        change too, not only editing one.
        """
        state: dict[str, str] = {}
        for pattern in self.patterns:
            matches = sorted(path for path in self.root.glob(pattern) if path.is_file())
            if not matches:
                state[pattern] = MISSING
            for path in matches:
                state[str(path.relative_to(self.root))] = _digest(path)
        return state

    def changes(self, baseline: dict[str, str]) -> list[str]:
        """Names whose content differs from ``baseline``, sorted."""
        current = self.snapshot()
        return sorted(
            name
            for name in current.keys() | baseline.keys()
            if current.get(name) != baseline.get(name)
        )


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return MISSING
