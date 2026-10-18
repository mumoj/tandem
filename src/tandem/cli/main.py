"""``tandem``: one command for the planner and for a human on a phone.

``tandem serve`` runs the supervisor. Every other command drives a running one
through its bridge, with the same verbs whoever is typing them. Output is terse
by default and the raw JSON response with ``--json``.

Exit codes are part of the interface, so a planner can branch without parsing:

====  ==========================================================
0     ok; for ``wait``, a result was written or the turn ended
1     the bridge refused the request (the message says why)
2     usage error, or an ambiguous request (several pending ids)
3     no running supervisor, or the bridge did not answer
4     ``wait``: hung
5     ``wait``: blocked on a permission or question
6     ``wait``: provider error or protected-file violation
7     ``wait``: the supervisor is stopping
8     ``wait``: reasoning stall
124   ``wait``: timed out while the implementer was still working
====  ==========================================================
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections import deque
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import BaseModel, ValidationError

from tandem.bridge.manifest import Manifest
from tandem.bridge.schemas import (
    DiffOut,
    DirectiveOut,
    MessagesOut,
    Ok,
    ResultOut,
    SpecOut,
    WaitOut,
)
from tandem.cli import render
from tandem.cli.client import BridgeClient, CliError, find_manifest
from tandem.config import Settings, default_state_root
from tandem.opencode.server import UnsupportedVersionError
from tandem.supervisor.supervisor import CostReport, Pending, StatusReport, WaitReason

app = typer.Typer(
    name="tandem",
    help="Supervise a headless OpenCode implementer for a frontier-model planner.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

WAIT_EXIT_CODES: dict[WaitReason, int] = {
    WaitReason.RESULT: 0,
    WaitReason.TURN_ENDED: 0,
    WaitReason.NO_SPEC: 0,
    WaitReason.HUNG: 4,
    WaitReason.BLOCKED: 5,
    WaitReason.ERROR: 6,
    WaitReason.PROTECTED_VIOLATION: 6,
    WaitReason.STOPPED: 7,
    WaitReason.REASONING_STALL: 8,
    WaitReason.TIMEOUT: 124,
}


@dataclass(frozen=True, slots=True)
class Target:
    state_root: Path
    task: str | None
    manifest: Path | None


@app.callback()
def main(
    ctx: typer.Context,
    task: Annotated[
        str | None,
        typer.Option(
            "--task",
            "-t",
            envvar="TANDEM_TASK",
            help="Task dir, state dir, or name prefix. Optional when one supervisor runs.",
        ),
    ] = None,
    manifest: Annotated[
        Path | None, typer.Option(envvar="TANDEM_MANIFEST", help="Address a bridge directly.")
    ] = None,
    state_root: Annotated[
        Path | None, typer.Option(envvar="TANDEM_STATE_ROOT", help="Default: XDG state dir.")
    ] = None,
) -> None:
    ctx.obj = Target(state_root or default_state_root(), task, manifest)


JsonOption = Annotated[bool, typer.Option("--json", help="Print the raw JSON response.")]


def _fail(exc: CliError) -> NoReturn:
    typer.echo(f"tandem: {exc}", err=True)
    raise typer.Exit(exc.exit_code)


@contextmanager
def bridge(ctx: typer.Context) -> Generator[BridgeClient, None, None]:
    """A client for the target supervisor; turns ``CliError`` into an exit code."""
    target: Target = ctx.obj
    try:
        path = find_manifest(target.state_root, task=target.task, manifest=target.manifest)
        client = BridgeClient(Manifest.read(path))
    except CliError as exc:
        _fail(exc)
    try:
        yield client
    except CliError as exc:
        _fail(exc)
    finally:
        client.close()


def _emit(data: BaseModel, as_json: bool, lines: list[str]) -> None:
    typer.echo(data.model_dump_json(indent=2) if as_json else "\n".join(lines))


def _read_text(words: list[str] | None, file: Path | None) -> str:
    """Text from ``--file`` (``-`` is stdin), from arguments, or piped on stdin."""
    if file is not None:
        return sys.stdin.read() if str(file) == "-" else file.read_text(encoding="utf-8")
    if words and words != ["-"]:
        return " ".join(words)
    if words == ["-"] or not sys.stdin.isatty():
        return sys.stdin.read()
    raise CliError("give the text as arguments, with --file, or on stdin", 2)


# ---- running the supervisor ------------------------------------------------


@app.command()
def serve(
    ctx: typer.Context,
    task_dir: Annotated[Path, typer.Argument(help="The working tree OpenCode edits.")],
    model: Annotated[
        str | None, typer.Option("--model", "-m", help="provider/model for the implementer.")
    ] = None,
    protect: Annotated[
        list[str] | None,
        typer.Option("--protect", "-p", help="Glob the implementer must not edit. Repeatable."),
    ] = None,
    notify_cmd: Annotated[
        str | None, typer.Option(help="Shell command run on each alert; summary in $TANDEM_NOTIFY.")
    ] = None,
    ntfy_topic: Annotated[str | None, typer.Option(help="Opt-in ntfy push topic.")] = None,
    bridge_port: Annotated[int | None, typer.Option(help="Default: a free port.")] = None,
    opencode_port: Annotated[int | None, typer.Option(help="Default: a free port.")] = None,
    opencode_bin: Annotated[str | None, typer.Option(help="Default: opencode on PATH.")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run the supervisor for TASK_DIR until `tandem finish` or Ctrl-C."""
    from tandem.runtime import run  # deferred: client commands should start fast

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname).1s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not verbose:
        # One line per request to OpenCode buries the journal lines that matter.
        logging.getLogger("httpx").setLevel(logging.WARNING)
    target: Target = ctx.obj
    overrides: dict[str, Any] = {
        "state_root": target.state_root,
        "model": model,
        "protected": protect,
        "notify_cmd": notify_cmd,
        "ntfy_topic": ntfy_topic,
        "bridge_port": bridge_port,
        "opencode_port": opencode_port,
        "opencode_bin": opencode_bin,
    }
    try:
        settings = Settings(
            task_dir=task_dir,
            **{key: value for key, value in overrides.items() if value is not None},
        )
    except ValidationError as exc:
        _fail(CliError("; ".join(str(error["msg"]) for error in exc.errors()), 2))

    def ready(manifest: Manifest) -> None:
        typer.echo(
            f"bridge   {manifest.url}  (OpenCode {manifest.opencode_version})\n"
            f"state    {manifest.state_dir}\n"
            f"drive it with: tandem --task {Path(manifest.state_dir).name} status",
            err=True,
        )

    try:
        asyncio.run(run(settings, ready=ready))
    except (UnsupportedVersionError, FileNotFoundError, RuntimeError) as exc:
        _fail(CliError(str(exc)))
    except ExceptionGroup as group:
        # A part of the supervisor died and took the rest down with it.
        logging.getLogger("tandem").error("supervisor stopped", exc_info=group)
        _fail(CliError("; ".join(f"{type(e).__name__}: {e}" for e in group.exceptions)))
    except KeyboardInterrupt:
        pass


# ---- specs and directives --------------------------------------------------


@app.command()
def spec(
    ctx: typer.Context,
    spec_id: Annotated[str, typer.Argument(help="Immutable id, e.g. S-12.")],
    text: Annotated[list[str] | None, typer.Argument(help="Spec text, or - for stdin.")] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="Read the spec from a file.")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Start SPEC_ID in a fresh session. Resending the same id and text is a no-op."""
    with bridge(ctx) as client:
        body = {"spec_id": spec_id, "text": _read_text(text, file)}
        out = client.post("/spec", SpecOut, body)
        _emit(out, as_json, [f"{out.spec_id} -> {out.session_id} [{out.status}]"])


class DirectiveKind(StrEnum):
    CORRECT = "correct"
    STOP = "stop"


@app.command()
def directive(
    ctx: typer.Context,
    spec_id: Annotated[str, typer.Argument(help="The spec this applies to; must be active.")],
    directive_id: Annotated[str, typer.Argument(help="Immutable id, e.g. D-3.")],
    kind: Annotated[DirectiveKind, typer.Argument(help="correct, or stop (abort and wait).")],
    text: Annotated[list[str] | None, typer.Argument()] = None,
    file: Annotated[Path | None, typer.Option("--file", "-f")] = None,
    as_json: JsonOption = False,
) -> None:
    """Send a correction, or stop the turn, for the active spec."""
    action = "CORRECT" if kind is DirectiveKind.CORRECT else "STOP_AND_WAIT"
    with bridge(ctx) as client:
        body = {
            "spec_id": spec_id,
            "directive_id": directive_id,
            "action": action,
            "text": _read_text(text, file),
        }
        out = client.post("/directive", DirectiveOut, body)
        _emit(out, as_json, [f"{out.directive_id} {out.action} delivered to {out.spec_id}"])


@app.command()
def retract(
    ctx: typer.Context,
    spec_id: Annotated[str, typer.Argument()],
    directive_id: Annotated[str, typer.Argument()],
    as_json: JsonOption = False,
) -> None:
    """Tell the implementer to disregard a directive."""
    with bridge(ctx) as client:
        body = {"spec_id": spec_id, "directive_id": directive_id}
        out = client.post("/retract", DirectiveOut, body)
        _emit(out, as_json, [f"{out.directive_id} retracted"])


@app.command()
def say(
    ctx: typer.Context,
    text: Annotated[list[str] | None, typer.Argument(help="Prompt text, or - for stdin.")] = None,
    as_json: JsonOption = False,
) -> None:
    """Send a free-form prompt to the active spec's session."""
    with bridge(ctx) as client:
        out = client.post("/prompt", SpecOut, {"text": _read_text(text, None)})
        _emit(out, as_json, [f"sent to {out.spec_id}"])


# ---- watching ---------------------------------------------------------------


@app.command()
def wait(
    ctx: typer.Context,
    timeout: Annotated[float, typer.Option(help="Seconds to wait, at most 3600.")] = 600,
    as_json: JsonOption = False,
) -> None:
    """Block until the implementer needs you. The exit code says why."""
    with bridge(ctx) as client:
        out = client.get("/wait", WaitOut, {"timeout": timeout}, timeout=timeout + 30)
        detail = f": {out.detail}" if out.detail else ""
        _emit(out, as_json, [f"{out.reason}{detail}"])
    raise typer.Exit(WAIT_EXIT_CODES.get(out.reason, 1))


@app.command()
def status(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """The active spec, its session, and what the implementer is doing."""
    with bridge(ctx) as client:
        out = client.get("/status", StatusReport)
        _emit(out, as_json, render.status(out))


@app.command()
def msg(
    ctx: typer.Context,
    count: Annotated[int, typer.Argument(help="How many messages.", min=1)] = 3,
    as_json: JsonOption = False,
) -> None:
    """The last few messages: what it said, and which tools it ran."""
    with bridge(ctx) as client:
        out = client.get("/messages", MessagesOut, {"limit": count})
        _emit(out, as_json, render.messages(out, count))


@app.command()
def pending(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Permission requests and questions waiting on a human."""
    with bridge(ctx) as client:
        out = client.get("/pending", Pending)
        _emit(out, as_json, render.pending(out))


@app.command()
def result(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """The implementer's result for the active spec, if it wrote one."""
    with bridge(ctx) as client:
        out = client.get("/result", ResultOut)
        found = out.result
        lines = (
            [f"{found.verdict}: {found.summary}"]
            if found
            else [f"no result yet (expected at {out.path})"]
        )
        _emit(out, as_json, lines)


@app.command()
def diff(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Files the active session changed."""
    with bridge(ctx) as client:
        out = client.get("/diff", DiffOut)
        _emit(out, as_json, render.diff(out))


@app.command()
def cost(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """Spend and tokens per spec, from OpenCode's own session totals."""
    with bridge(ctx) as client:
        out = client.get("/cost", CostReport)
        _emit(out, as_json, render.cost(out))


# ---- releasing a blocked session --------------------------------------------


def _single_pending(client: BridgeClient, kind: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    waiting = client.get("/pending", Pending)
    ids = (
        [p.id for p in waiting.permissions]
        if kind == "permissions"
        else [q.id for q in waiting.questions]
    )
    if len(ids) == 1:
        return ids[0]
    raise CliError(f"{len(ids)} pending {kind}; pass an id explicitly", 2)


def _reply(ctx: typer.Context, reply: str, request_id: str | None, message: str | None) -> None:
    with bridge(ctx) as client:
        chosen = _single_pending(client, "permissions", request_id)
        body: dict[str, Any] = {"id": chosen, "reply": reply}
        if message:
            body["message"] = message
        typer.echo(client.post("/permission", Ok, body).detail)


RequestId = Annotated[str | None, typer.Argument(help="Default: the only pending one.")]
Reason = Annotated[str | None, typer.Option("--message", help="Reason passed to the model.")]


@app.command()
def allow(ctx: typer.Context, request_id: RequestId = None, message: Reason = None) -> None:
    """Allow a pending permission once."""
    _reply(ctx, "once", request_id, message)


@app.command()
def always(ctx: typer.Context, request_id: RequestId = None) -> None:
    """Allow a pending permission for the rest of the session. Prefer `allow`."""
    _reply(ctx, "always", request_id, None)


@app.command()
def deny(ctx: typer.Context, request_id: RequestId = None, message: Reason = None) -> None:
    """Reject a pending permission."""
    _reply(ctx, "reject", request_id, message)


@app.command()
def answer(
    ctx: typer.Context,
    label: Annotated[list[str], typer.Argument(help="The option label to select.")],
    request_id: Annotated[str | None, typer.Option("--id")] = None,
) -> None:
    """Answer the pending question by selecting one option label."""
    with bridge(ctx) as client:
        chosen = _single_pending(client, "questions", request_id)
        body = {"id": chosen, "answers": [[" ".join(label)]]}
        typer.echo(client.post("/answer", Ok, body).detail)


@app.command()
def reject(ctx: typer.Context, request_id: RequestId = None) -> None:
    """Decline the pending question."""
    with bridge(ctx) as client:
        chosen = _single_pending(client, "questions", request_id)
        typer.echo(client.post("/answer", Ok, {"id": chosen, "reject": True}).detail)


@app.command()
def abort(ctx: typer.Context) -> None:
    """Stop the current turn. The session stays open."""
    with bridge(ctx) as client:
        typer.echo(client.post("/abort", Ok).detail)


@app.command()
def finish(ctx: typer.Context) -> None:
    """Stop the supervisor, capturing evidence on the way out."""
    with bridge(ctx) as client:
        typer.echo(client.post("/finish", Ok).detail)


# ---- local files --------------------------------------------------------------


@app.command()
def log(
    ctx: typer.Context,
    lines: Annotated[int, typer.Argument(min=1)] = 20,
    stream: Annotated[
        bool, typer.Option("--stream", help="The raw event stream instead of the journal.")
    ] = False,
) -> None:
    """The last lines of the journal, or of the raw event stream."""
    with bridge(ctx) as client:
        root = Path(client.manifest.state_dir)
    path = root / ("events.sse" if stream else "journal.jsonl")
    if not path.exists():
        typer.echo(f"nothing at {path} yet")
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in deque(handle, maxlen=lines):
            typer.echo(line.rstrip("\n")[:400])


@app.command()
def where(ctx: typer.Context) -> None:
    """The bridge URL, state directory and process of the target supervisor."""
    with bridge(ctx) as client:
        m = client.manifest
        typer.echo(
            f"bridge   {m.url}\nstate    {m.state_dir}\ntask     {m.task_dir}\n"
            f"pid      {m.pid}\nopencode {m.opencode_version} on port {m.opencode_port}"
        )
