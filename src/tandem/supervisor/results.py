"""The implementer's machine-readable result.

The first version asked the model to append a Markdown heading, ``## Agent
Result (Iteration N)``, and matched it with a regex. The model wrote ``## Agent
Result — <spec-id>`` instead, which is the natural wording, the regex missed it,
and the miss looked like a corrupt channel. A structured signal belongs in a
structured file. It is also written as the work happens rather than composed at
the end of the turn, so a provider failure on the last generation does not lose
the outcome.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError


class ImplementerResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    spec_id: str
    verdict: str
    summary: str = ""


def read_result(path: Path, spec_id: str) -> ImplementerResult | None:
    """The result for ``spec_id``, or ``None`` when it cannot be trusted yet.

    Absent, mid-write, malformed, or naming another spec are all normal states
    on the way to a real result, and the next monitor tick tries again, so none
    of them is an error.
    """
    try:
        result = ImplementerResult.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        return None
    # A result left by an earlier spec must not end this one.
    return result if result.spec_id == spec_id else None
