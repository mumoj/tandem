"""Typed reads from untyped JSON.

Event payloads stay plain dicts (see ``events.Event``), so reading one means
walking nested keys and checking types at every step. These helpers do that
once, so call sites read as ``dig(part, "state", "status")``.
"""

from __future__ import annotations

from typing import cast


def dig(data: object, *keys: str) -> object:
    """Follow ``keys`` through nested dicts; ``None`` if any step is missing."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = cast(dict[str, object], data).get(key)
    return data


def dig_str(data: object, *keys: str) -> str | None:
    value = dig(data, *keys)
    return value if isinstance(value, str) else None


def dig_list(data: object, *keys: str) -> list[object]:
    value = dig(data, *keys)
    return cast(list[object], value) if isinstance(value, list) else []
