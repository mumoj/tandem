"""The text the implementer receives.

Every prompt opens with a marker line. After a restart, the supervisor cannot
know whether a prompt it was sending reached the session, so it looks for the
marker in the session's messages before sending again. That makes delivery
idempotent: at most one copy of each spec or directive, however many times the
supervisor restarts.
"""

from __future__ import annotations

from pathlib import Path

from tandem.supervisor.state import DirectiveAction

SPEC_MARKER = "TANDEM_SPEC_ID"
DIRECTIVE_MARKER = "TANDEM_DIRECTIVE_ID"
RETRACTION_MARKER = "TANDEM_RETRACTION"


def spec_marker(spec_id: str) -> str:
    return f"{SPEC_MARKER}: {spec_id}"


def directive_marker(directive_id: str) -> str:
    return f"{DIRECTIVE_MARKER}: {directive_id}"


def spec_prompt(spec_id: str, text: str, result_path: Path, protected: tuple[str, ...]) -> str:
    keep_out = (
        f"\nDo not edit these files, by any means: {', '.join(protected)}.\n" if protected else ""
    )
    return f"""{spec_marker(spec_id)}

You are the implementer for one immutable specification, `{spec_id}`. Work only on
what it asks for.

<specification>
{text.strip()}
</specification>
{keep_out}
Report the outcome by writing JSON to {result_path}:

    {{"spec_id": "{spec_id}", "verdict": "done", "summary": "<what you did, how you checked>"}}

Use "done" when the specification is met and checked, "blocked" when something
outside your control stops you, and "failed" when you could not meet it. Write
the file as soon as you know the outcome, before any final summary, then end
your turn. Paste command output into the summary rather than describing it.
A new specification arrives in a fresh session.
"""


def directive_prompt(directive_id: str, action: DirectiveAction, text: str) -> str:
    if action is DirectiveAction.STOP_AND_WAIT:
        instruction = (
            "Your previous turn was stopped. Make no further changes. Update the "
            'result file with verdict "blocked" and what you had done so far, '
            "then end your turn."
        )
    else:
        instruction = (
            "Apply this correction to the current specification, update the result "
            "file when the outcome is known, then end your turn."
        )
    return f"""{directive_marker(directive_id)}

A {action.value} directive for the current specification.

<directive>
{text.strip()}
</directive>

{instruction}
"""


def retraction_prompt(directive_ids: list[str]) -> str:
    joined = ", ".join(directive_ids)
    return f"""{RETRACTION_MARKER}: {joined}

These directives are retracted: {joined}. Do not act on them. If you already
applied one, say so, and continue with the specification as it stood before it.
"""
