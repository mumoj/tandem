"""tandem as an MCP server, so a planner never has to learn the CLI.

Register it once (``claude mcp add tandem -- tandem mcp``) and the planner gets
typed tools: start an implementer, hand it a spec, wait, review, reply. The
supervisor is started on first use for the project the planner is working in,
outlives the MCP server, and is found again by directory; nobody types
``tandem serve`` or ``--task``. Every reply carries the dashboard link, so the
human can watch from a browser without knowing tandem exists.

Several implementers work on one repository at once through spawn_implementer:
each gets its own git worktree, branch and supervisor, wait_any wakes the
planner when any of them needs it, and land_implementer reviews, merges or
discards each one's work. Inside one spec, an implementer may also start
OpenCode subagents of its own; their prompts and activity count toward the spec.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
import anyio.from_thread
import anyio.to_thread
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from tandem import worktrees
from tandem.bridge.manifest import Manifest
from tandem.bridge.schemas import (
    DiffOut,
    DirectiveOut,
    MessagesOut,
    Ok,
    OverviewOut,
    ResultOut,
    SpecOut,
    WaitOut,
)
from tandem.cli.client import BridgeClient, CliError
from tandem.config import TaskPaths, default_state_root
from tandem.hub import Hub
from tandem.opencode.host import HostError, SharedHost, reap_orphans
from tandem.opencode.server import resolve_binary
from tandem.supervisor.supervisor import Pending, StatusReport, WaitReason

INSTRUCTIONS = """\
tandem runs a cheaper implementer model in OpenCode while you plan and review.
You write specs; it writes the code. Do not do its work, and do not take its word.

Loop:
1. submit_spec with one outcome, the files it may touch, and how to check it is done.
   Spec IDs are immutable: a changed requirement is a new ID. Pick the implementer with
   `model` (provider/model) and `agent`; list_models shows what is available. The
   implementer starts on first use in the current project; start_implementer only to
   set defaults or protected files.
2. wait, and act on `reason` (`next_step` says what to do). On `timeout`, wait again.
3. On `result`, review: the result is a claim. Check get_result's diff, read the
   files, run the tests yourself. Then correct (send_directive) or move on (next spec).
4. On `blocked`, get_pending, then reply_permission or answer_question. Never allow an
   edit to a protected file.
5. finish when the task is done.

Give the human the dashboard link from the first reply: it shows the implementer live.
With several implementers, give them the `overview` link instead: one page for all of them.

Parallel work, for independent pieces of one task:
- spawn_implementer once per piece, each with its own name and spec. Each works in its
  own git worktree and branch, so they cannot overwrite each other.
- wait_any instead of wait: it returns as soon as any implementer needs you, and names it.
- Every other tool takes the implementer's name as `workdir`.
- land_implementer: review its diff, then merge it into the current branch or discard it.
  A finished implementer stays ready until it is landed.
"""

NEXT_STEP: dict[WaitReason, str] = {
    WaitReason.RESULT: "Review: get_result, then read the changed files and run the tests.",
    WaitReason.TURN_ENDED: "The turn ended without a result: read get_messages, then "
    "send_directive or say.",
    WaitReason.BLOCKED: "get_pending, then reply_permission or answer_question.",
    WaitReason.REASONING_STALL: "Read get_messages and look for a contradiction in the spec.",
    WaitReason.HUNG: "Check status; abort and re-prompt if it is stuck.",
    WaitReason.ERROR: "Provider error: check status and get_messages; it may need a "
    "different model.",
    WaitReason.PROTECTED_VIOLATION: "A protected file changed and the spec stopped. "
    "Restore the file before the next spec.",
    WaitReason.NO_SPEC: "Nothing to wait for: submit_spec first.",
    WaitReason.STOPPED: "The supervisor is stopping.",
    WaitReason.TIMEOUT: "Still working: wait again.",
}

START_TIMEOUT = 60.0
_CHILDREN: dict[Path, subprocess.Popen[bytes]] = {}
"""Supervisors this server started, kept so their exit can be reaped."""
# Clients cut long tool calls off (Codex at 60 s by default), so a wait returns
# before that and the planner calls it again. wait is level-triggered, so
# nothing is missed between calls.
DEFAULT_WAIT = 50.0


@dataclass
class _Connection:
    """What lives exactly as long as one MCP connection."""

    hub: Hub | None = None


_CONNECTION = _Connection()


def _shared() -> bool:
    """Whether implementers share one server and one OpenCode. Default: yes."""
    return os.environ.get("TANDEM_OPENCODE_MODE", "shared") != "isolated"


def _new_opencode() -> SharedHost:
    records = _state_root() / "hosts"
    binary = resolve_binary(os.environ.get("TANDEM_OPENCODE_BIN", "opencode"))
    host = SharedHost(binary, log_path=records / f"{os.getpid()}.log", records=records)
    # Called on the event loop's thread, which lives as long as this process.
    host.start(die_with_parent=True)
    return host


@contextlib.asynccontextmanager
async def _lifespan(_server: MCPServer[None]) -> AsyncGenerator[None]:
    """Start the hub when the planner connects; stop it, and every task, when it goes."""
    if not _shared():
        yield
        return
    reap_orphans(_state_root() / "hosts")
    hub = Hub(_state_root(), _new_opencode)
    await hub.start()
    _CONNECTION.hub = hub
    try:
        yield
    finally:
        _CONNECTION.hub = None
        await hub.close()


def _manifests() -> list[Manifest]:
    """The manifest of every supervisor running on this machine."""
    found: list[Manifest] = []
    for path in sorted(_state_root().glob("*/manifest.json")):
        try:
            manifest = Manifest.read(path)
        except (OSError, ValueError):
            continue
        if manifest.alive:
            found.append(manifest)
    return found


server = MCPServer("tandem", instructions=INSTRUCTIONS, lifespan=_lifespan)

Workdir = Annotated[
    str | None,
    Field(
        description="The project directory, or the name of a spawned implementer. "
        "Default: this project."
    ),
]
Name = Annotated[
    str,
    Field(description="Short implementer name: lower-case letters, digits, dashes."),
]
REVIEW_CHARS = 40_000
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


def _state_root() -> Path:
    configured = os.environ.get("TANDEM_STATE_ROOT")
    return Path(configured).expanduser() if configured else default_state_root()


def _base() -> Path:
    configured = os.environ.get("TANDEM_WORKDIR")
    return Path(configured) if configured else Path.cwd()


def _task_dir(workdir: str | None) -> Path:
    base = _base()
    path = (base / workdir if workdir else base).expanduser().resolve()
    if path.is_dir():
        return path
    if workdir and worktrees.NAME.fullmatch(workdir):
        # Not a directory, so maybe a spawned implementer's name.
        with contextlib.suppress(worktrees.GitError):
            repo = worktrees.repo_root(base)
            return worktrees.find_worktree(repo, workdir).path
    raise ToolError(f"no such directory or implementer: {workdir or path}")


def _repo(workdir: str | None) -> Path:
    try:
        return worktrees.repo_root(_task_dir(workdir))
    except worktrees.GitError as exc:
        raise ToolError(str(exc)) from exc


@contextlib.contextmanager
def _git_errors() -> Generator[None, None, None]:
    try:
        yield
    except worktrees.GitError as exc:
        raise ToolError(str(exc)) from exc


def _stop(task_dir: Path, timeout: float = 30.0) -> bool:
    """Finish the supervisor for ``task_dir`` and wait for it to exit. False if none ran."""
    manifest = _running(task_dir)
    if manifest is None:
        return False
    with contextlib.suppress(CliError), contextlib.closing(BridgeClient(manifest)) as client:
        client.post("/finish", Ok)
    path = TaskPaths.for_task(_state_root(), task_dir).manifest
    deadline = time.monotonic() + timeout
    # A clean exit removes the manifest. The pid alone is not enough: a supervisor
    # this server started stays a zombie, and so "alive", until it is reaped.
    while path.exists() and manifest.alive and time.monotonic() < deadline:
        _reap()
        time.sleep(0.25)
    _reap()
    if path.exists() and manifest.alive:
        raise ToolError(f"the implementer in {task_dir} did not stop within {timeout:.0f}s")
    return True


def _reap() -> None:
    """Collect the exit status of supervisors this server started and that have stopped."""
    for task_dir, process in list(_CHILDREN.items()):
        if process.poll() is not None:
            del _CHILDREN[task_dir]


def _archive_state(task_dir: Path) -> Path | None:
    """Move a finished implementer's state aside, keeping it as evidence.

    State is keyed by directory, and a later implementer with the same name gets
    the same directory: left in place, its specs and results would be taken for
    the new one's.
    """
    root = TaskPaths.for_task(_state_root(), task_dir).root
    if not root.exists():
        return None
    archive = _state_root() / "landed" / f"{root.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root.rename(archive)


def _running(task_dir: Path) -> Manifest | None:
    path = TaskPaths.for_task(_state_root(), task_dir).manifest
    try:
        manifest = Manifest.read(path)
    except (OSError, ValueError):
        return None
    return manifest if manifest.alive else None


def _spawn(task_dir: Path, model: str | None, protect: list[str] | None) -> Manifest:
    """Start supervising ``task_dir``: on the hub, or as a ``tandem serve`` of its own."""
    if (hub := _CONNECTION.hub) is not None:
        start = functools.partial(hub.start_task, task_dir, model=model, protect=protect)
        try:
            return anyio.from_thread.run(start)
        except (HostError, RuntimeError, ValueError) as exc:
            raise ToolError(f"the implementer failed to start: {exc}") from exc
    return _spawn_process(task_dir, model, protect)


def _spawn_process(task_dir: Path, model: str | None, protect: list[str] | None) -> Manifest:
    """``tandem serve`` for ``task_dir``, detached, once it answers. Isolated mode."""
    paths = TaskPaths.for_task(_state_root(), task_dir)
    paths.ensure()
    argv = [sys.executable, "-m", "tandem", "--state-root", str(_state_root())]
    argv += ["serve", str(task_dir)]
    if model:
        argv += ["--model", model]
    for pattern in protect or []:
        argv += ["--protect", pattern]
    log_path = paths.root / "serve.log"
    with log_path.open("ab") as log:
        # A session of its own: the supervisor outlives this MCP server, and a
        # planner restart finds it again by directory.
        process = subprocess.Popen(
            argv,
            cwd=task_dir,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    _CHILDREN[task_dir] = process
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        manifest = _running(task_dir)
        if manifest is not None and manifest.pid == process.pid:
            return manifest
        if process.poll() is not None:
            raise ToolError(f"the implementer failed to start: {_tail(log_path)}")
        time.sleep(0.25)
    raise ToolError(f"the implementer did not start within {START_TIMEOUT:.0f}s; see {log_path}")


def _tail(path: Path, lines: int = 8) -> str:
    with contextlib.suppress(OSError):
        return " | ".join(path.read_text(errors="replace").strip().splitlines()[-lines:])
    return "(no output)"


@contextlib.contextmanager
def _bridge(workdir: str | None, *, start: bool = False) -> Generator[BridgeClient, None, None]:
    """A client for the implementer in ``workdir``, started first if ``start``."""
    task_dir = _task_dir(workdir)
    manifest = _running(task_dir)
    if manifest is None:
        if not start:
            raise ToolError(f"no implementer running in {task_dir}; submit_spec starts one")
        manifest = _spawn(task_dir, None, None)
    client = BridgeClient(manifest)
    try:
        yield client
    except CliError as exc:
        raise ToolError(str(exc)) from exc
    finally:
        client.close()


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _links(manifest: Manifest) -> dict[str, str]:
    """The dashboard for one implementer, and the overview of all of them if there is one."""
    links = {"dashboard": manifest.dashboard_url}
    if (hub := _CONNECTION.hub) is not None:
        links["overview"] = hub.dashboard_url
    return links


# ---- starting ---------------------------------------------------------------


@server.tool(annotations=WRITE)
def start_implementer(
    workdir: Workdir = None,
    model: Annotated[
        str | None, Field(description="Default provider/model for specs that do not name one.")
    ] = None,
    protect: Annotated[
        list[str] | None,
        Field(description="Globs, relative to workdir, the implementer must not edit."),
    ] = None,
) -> dict[str, Any]:
    """Start the implementer for a project, or report the one already running.

    Optional: submit_spec starts one with OpenCode's defaults. Use this to set a
    default model or protected files; both are fixed until the implementer is
    finished, though each spec can still pick its own model.
    """
    task_dir = _task_dir(workdir)
    manifest = _running(task_dir)
    already = manifest is not None
    if manifest is None:
        manifest = _spawn(task_dir, model, protect)
    note = (
        "Already running; its default model and protected files are unchanged. "
        "Pass `model` per spec, or finish and start again to change them."
        if already and (model or protect)
        else ""
    )
    return {
        "task_dir": str(task_dir),
        "already_running": already,
        "dashboard": manifest.dashboard_url,
        "note": note,
    }


@server.tool(annotations=READ)
def list_models() -> dict[str, Any]:
    """The models and agents OpenCode can run, for submit_spec's `model` and `agent`.

    A listed model can still be refused by the provider account: a ChatGPT login,
    for one, accepts only some OpenAI models. A spec that fails that way returns
    `error` from wait; resubmit under a new Spec ID with another model.
    """
    binary = os.environ.get("TANDEM_OPENCODE_BIN", "opencode")
    models = _opencode_lines([binary, "models"])
    agents: list[dict[str, str]] = []
    for line in _opencode_lines([binary, "agent", "list"]):
        name, _, mode = line.partition(" (")
        if mode.endswith(")") and name.isidentifier():
            agents.append({"name": name, "mode": mode[:-1]})
    return {
        "models": [m for m in models if "/" in m],
        "agents": agents,
        "hint": "Primary agents (build, plan) run a spec; subagents are what the "
        "implementer can delegate to itself through its task tool.",
    }


def _opencode_lines(argv: list[str]) -> list[str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError(f"could not run {argv[0]}: {exc}") from exc
    if done.returncode != 0:
        raise ToolError(f"{' '.join(argv)} failed: {done.stderr.strip()[:300]}")
    return [line for line in done.stdout.splitlines() if line and not line.startswith(" ")]


@server.tool(annotations=READ)
def list_implementers() -> list[dict[str, Any]]:
    """Every implementer running on this machine, one per project or worktree."""
    found: list[dict[str, Any]] = []
    for manifest in _manifests():
        entry: dict[str, Any] = {"task_dir": manifest.task_dir, "dashboard": manifest.dashboard_url}
        with contextlib.suppress(CliError), contextlib.closing(BridgeClient(manifest)) as client:
            status = client.get("/status", StatusReport)
            entry |= {"spec_id": status.spec_id, "activity": status.activity.activity}
        found.append(entry)
    return found


@server.tool(annotations=READ)
def dashboard(workdir: Workdir = None) -> str:
    """The browser link that shows the implementer live, for the human."""
    with _bridge(workdir) as client:
        return client.manifest.dashboard_url


# ---- parallel implementers ----------------------------------------------------


@server.tool(annotations=WRITE)
def spawn_implementer(
    name: Name,
    text: Annotated[str, Field(description="Its spec: one outcome, files, how to check.")],
    model: Annotated[str | None, Field(description="provider/model for this implementer.")] = None,
    agent: Annotated[str | None, Field(description="OpenCode agent, e.g. build.")] = None,
    spec_id: Annotated[
        str, Field(description="Immutable spec id within this implementer.")
    ] = "S-1",
    protect: Annotated[
        list[str] | None, Field(description="Globs this implementer must not edit.")
    ] = None,
    base: Annotated[
        str | None, Field(description="Commit or branch to start from. Default: HEAD.")
    ] = None,
    workdir: Workdir = None,
) -> dict[str, Any]:
    """Start one of several implementers, in its own git worktree and branch, on a spec.

    Retrying with the same name is safe: the worktree and supervisor are reused and
    the spec is idempotent. Use the name as `workdir` in every other tool.
    """
    repo = _repo(workdir)
    with _git_errors():
        fresh = all(w.name != name for w in worktrees.list_worktrees(repo))
        worktree = worktrees.create_worktree(repo, _state_root(), name, base)
        if fresh and _running(worktree.path) is None:
            _archive_state(worktree.path)
        dirty = bool(worktrees.git(repo, "status", "--porcelain", "--untracked-files=no").strip())
    manifest = _running(worktree.path) or _spawn(worktree.path, None, protect)
    body: dict[str, Any] = {"spec_id": spec_id, "text": text}
    body |= {key: value for key, value in (("model", model), ("agent", agent)) if value}
    with contextlib.closing(BridgeClient(manifest)) as client:
        try:
            out = client.post("/spec", SpecOut, body)
        except CliError as exc:
            raise ToolError(str(exc)) from exc
    return _dump(out) | {
        "name": name,
        "branch": worktree.branch,
        "worktree": str(worktree.path),
        **_links(manifest),
        "note": "The repository has uncommitted changes to tracked files; this implementer "
        "starts from "
        f"{base or 'HEAD'} without them."
        if dirty
        else "",
    }


class ImplementerState(BaseModel):
    """One spawned implementer, as wait_any sees it."""

    name: str
    state: str
    """``working``, ``stopped``, or the reason ``wait`` would give: result, blocked, ..."""
    spec_id: str | None = None
    detail: str = ""
    next_step: str = ""

    @property
    def needs_you(self) -> bool:
        return self.state not in ("working", "stopped")


class WaitAnyOut(BaseModel):
    reason: Literal["ready", "all_stopped", "timeout"]
    ready: list[ImplementerState]
    """Every implementer that needs the planner now, not only the first."""
    working: list[str]
    stopped: list[str]


def _observe(worktree: worktrees.Worktree) -> ImplementerState:
    manifest = _running(worktree.path)
    if manifest is None:
        return ImplementerState(name=worktree.name, state="stopped")
    try:
        with contextlib.closing(BridgeClient(manifest, timeout=10)) as client:
            overview = client.get("/overview", OverviewOut)
    except CliError as exc:
        return ImplementerState(name=worktree.name, state="stopped", detail=str(exc))
    reason = overview.attention
    return ImplementerState(
        name=worktree.name,
        state=reason or "working",
        spec_id=overview.status.spec_id,
        detail=overview.attention_detail,
        next_step=NEXT_STEP.get(reason, "") if reason else "",
    )


def _survey(repo: Path, names: list[str] | None) -> list[ImplementerState]:
    with _git_errors():
        spawned = worktrees.list_worktrees(repo)
    if names is not None:
        spawned = [w for w in spawned if w.name in names]
    if not spawned:
        raise ToolError("no spawned implementers; spawn_implementer first")
    return [_observe(worktree) for worktree in spawned]


def _describe(state: ImplementerState) -> str:
    match state.state:
        case "working":
            return f"{state.name} is working on {state.spec_id}"
        case "stopped":
            return f"{state.name} stopped"
        case "result" | "turn_ended":
            return f"{state.name} finished {state.spec_id}: {state.state}"
        case _:
            return f"{state.name} needs you: {state.state}"


@server.tool(annotations=READ)
async def wait_any(
    ctx: Context,
    # A tool argument named like wait's, not a cancellation scope: the loop below
    # returns at the deadline with what it saw, which fail_after would discard.
    timeout: Annotated[  # noqa: ASYNC109
        float, Field(gt=0, le=3600, description="Seconds to block. Keep under your client's limit.")
    ] = DEFAULT_WAIT,
    names: Annotated[
        list[str] | None, Field(description="Only these implementers. Default: all spawned.")
    ] = None,
    workdir: Workdir = None,
) -> WaitAnyOut:
    """Block until any spawned implementer needs you, or `timeout` passes.

    Each implementer that finishes, blocks or stops is announced as it happens, as a
    progress message on this call. The reply lists every implementer that needs
    attention. Level-triggered: a finished implementer stays in `ready` until it is
    landed or given a new spec.
    """
    repo = _repo(workdir)
    started = anyio.current_time()
    seen: dict[str, str] = {}
    while True:
        states = await anyio.to_thread.run_sync(_survey, repo, names)
        elapsed = anyio.current_time() - started
        for state in states:
            if seen.get(state.name, "working") != state.state:
                message = _describe(state)
                await ctx.report_progress(min(elapsed, timeout), timeout, message)
            seen[state.name] = state.state
        ready = [s for s in states if s.needs_you]
        working = [s.name for s in states if s.state == "working"]
        if ready or not working or elapsed >= timeout:
            return WaitAnyOut(
                reason="ready" if ready else "all_stopped" if not working else "timeout",
                ready=ready,
                working=working,
                stopped=[s.name for s in states if s.state == "stopped"],
            )
        await anyio.sleep(1.0)


@server.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
def land_implementer(
    name: Name,
    action: Annotated[
        Literal["review", "merge", "discard"],
        Field(
            description="review: show what merging would bring in. merge: commit its work and "
            "merge it into the repository's current branch. discard: throw its work away."
        ),
    ] = "review",
    message: Annotated[
        str | None, Field(description="Commit message for its work, when merging.")
    ] = None,
    workdir: Workdir = None,
) -> dict[str, Any]:
    """Review a spawned implementer's work, then merge it or discard it.

    merge and discard stop its supervisor and remove its worktree and branch. Review
    first: the diff is the evidence, not the implementer's result.
    """
    repo = _repo(workdir)
    with _git_errors():
        worktree = worktrees.find_worktree(repo, name)
        manifest = _running(worktree.path)
        status: StatusReport | None = None
        if manifest is not None:
            with contextlib.suppress(CliError), contextlib.closing(BridgeClient(manifest)) as c:
                status = c.get("/status", StatusReport)
        if action == "review":
            return worktrees.review(repo, worktree, REVIEW_CHARS) | {
                "spec_id": status.spec_id if status else None,
                "result": _dump(status.result) if status and status.result else None,
                "running": manifest is not None,
            }
        if action == "merge" and status is not None and status.activity.busy:
            raise ToolError(f"{name} is still working; wait for it, or abort it first")
        _stop(worktree.path)
        if action == "discard":
            worktrees.remove(repo, worktree)
            return {"name": name, "discarded": True, "evidence": _evidence(worktree)}
        committed = worktrees.commit_all(worktree, message or f"tandem: {name}")
        into = worktrees.merge(repo, worktree, f"Merge {worktree.branch}")
        worktrees.remove(repo, worktree)
        return {
            "name": name,
            "merged_into": into,
            "committed_leftovers": committed,
            "evidence": _evidence(worktree),
        }


def _evidence(worktree: worktrees.Worktree) -> str | None:
    archived = _archive_state(worktree.path)
    return str(archived) if archived else None


# ---- specs ------------------------------------------------------------------


@server.tool(annotations=WRITE)
def submit_spec(
    spec_id: Annotated[
        str, Field(description="Immutable id, e.g. S-1. A changed spec is a new id.")
    ],
    text: Annotated[
        str, Field(description="The spec: one outcome, files it may touch, how to check.")
    ],
    model: Annotated[
        str | None, Field(description="provider/model for this spec, e.g. openai/gpt-5.6-luna.")
    ] = None,
    agent: Annotated[
        str | None, Field(description="OpenCode agent to run it, e.g. build. Default: OpenCode's.")
    ] = None,
    workdir: Workdir = None,
) -> dict[str, Any]:
    """Hand the implementer a spec in a fresh session, starting the implementer if needed.

    Resending the same id, text, model and agent is a no-op, so retrying is safe.
    """
    body: dict[str, Any] = {"spec_id": spec_id, "text": text}
    body |= {key: value for key, value in (("model", model), ("agent", agent)) if value}
    with _bridge(workdir, start=True) as client:
        out = client.post("/spec", SpecOut, body)
        return _dump(out) | _links(client.manifest)


@server.tool(annotations=READ)
def wait(
    timeout: Annotated[
        float, Field(gt=0, le=3600, description="Seconds to block. Keep under your client's limit.")
    ] = DEFAULT_WAIT,
    workdir: Workdir = None,
) -> dict[str, Any]:
    """Block until the implementer needs you, or `timeout` passes. Call again on `timeout`."""
    with _bridge(workdir) as client:
        out = client.get("/wait", WaitOut, {"timeout": timeout}, timeout=timeout + 30)
    return {
        "reason": out.reason,
        "detail": out.detail,
        "next_step": NEXT_STEP.get(out.reason, ""),
        "spec_id": out.status.spec_id,
        "activity": _dump(out.status.activity),
    }


@server.tool(annotations=READ)
def status(workdir: Workdir = None) -> dict[str, Any]:
    """The active spec, its model and agent, and what the implementer is doing now."""
    with _bridge(workdir) as client:
        return _dump(client.get("/status", StatusReport)) | {
            "dashboard": client.manifest.dashboard_url
        }


@server.tool(annotations=READ)
def get_result(workdir: Workdir = None) -> dict[str, Any]:
    """The implementer's result for the active spec, and the files its session changed.

    The result is the implementer's own claim. Read the files and run the checks yourself.
    """
    with _bridge(workdir) as client:
        result = client.get("/result", ResultOut)
        diff = client.get("/diff", DiffOut)
    return {"result": _dump(result)["result"], "changed_files": _dump(diff)["files"]}


@server.tool(annotations=READ)
def get_messages(
    limit: Annotated[int, Field(gt=0, le=50, description="How many recent messages.")] = 3,
    workdir: Workdir = None,
) -> dict[str, Any]:
    """The implementer's last few messages: what it said and which tools it ran."""
    with _bridge(workdir) as client:
        return _dump(client.get("/messages", MessagesOut, {"limit": limit}))


@server.tool(annotations=WRITE)
def send_directive(
    spec_id: Annotated[str, Field(description="The active spec this directive belongs to.")],
    directive_id: Annotated[str, Field(description="Immutable id, e.g. D-1.")],
    text: Annotated[str, Field(description="What to change, or why to stop.")],
    action: Annotated[
        Literal["correct", "stop"],
        Field(description="correct: fix and carry on. stop: abort the turn and wait."),
    ] = "correct",
    workdir: Workdir = None,
) -> dict[str, Any]:
    """Correct the implementer's work on the active spec, or stop it."""
    body = {
        "spec_id": spec_id,
        "directive_id": directive_id,
        "action": "CORRECT" if action == "correct" else "STOP_AND_WAIT",
        "text": text,
    }
    with _bridge(workdir) as client:
        return _dump(client.post("/directive", DirectiveOut, body))


@server.tool(annotations=WRITE)
def say(
    text: Annotated[str, Field(description="A free-form message to the implementer.")],
    workdir: Workdir = None,
) -> dict[str, Any]:
    """Send a free-form message to the active spec's session. Prefer send_directive."""
    with _bridge(workdir) as client:
        return _dump(client.post("/prompt", SpecOut, {"text": text}))


# ---- unblocking -------------------------------------------------------------


@server.tool(annotations=READ)
def get_pending(workdir: Workdir = None) -> dict[str, Any]:
    """Permission requests and questions waiting on a decision, including the diff."""
    with _bridge(workdir) as client:
        return _dump(client.get("/pending", Pending))


@server.tool(annotations=WRITE)
def reply_permission(
    decision: Annotated[
        Literal["allow", "always", "deny"],
        Field(description="allow once, always (rest of the session), or deny."),
    ],
    message: Annotated[
        str | None, Field(description="Why, sent to the implementer. Useful with deny.")
    ] = None,
    request_id: Annotated[
        str | None, Field(description="Which request; needed only when several are pending.")
    ] = None,
    workdir: Workdir = None,
) -> str:
    """Release a permission request. Never allow an edit to a protected file."""
    reply = {"allow": "once", "always": "always", "deny": "reject"}[decision]
    with _bridge(workdir) as client:
        target = request_id or _only(client.get("/pending", Pending).permissions, "permission")
        body = {"id": target, "reply": reply, "message": message}
        return client.post("/permission", Ok, body).detail


@server.tool(annotations=WRITE)
def answer_question(
    answers: Annotated[
        list[list[str]] | None,
        Field(description="Chosen option labels, one list per question asked."),
    ] = None,
    reject: Annotated[bool, Field(description="Decline the question instead.")] = False,
    request_id: Annotated[
        str | None, Field(description="Which question; needed only when several are pending.")
    ] = None,
    workdir: Workdir = None,
) -> str:
    """Answer the implementer's question by option labels, or decline it."""
    if reject == bool(answers):
        raise ToolError("give either answers or reject=true")
    with _bridge(workdir) as client:
        target = request_id or _only(client.get("/pending", Pending).questions, "question")
        body: dict[str, Any] = {"id": target}
        body |= {"reject": True} if reject else {"answers": answers}
        return client.post("/answer", Ok, body).detail


def _only(requests: list[Any], kind: str) -> str:
    if len(requests) == 1:
        return str(requests[0].id)
    if not requests:
        raise ToolError(f"no {kind} is pending")
    ids = ", ".join(str(r.id) for r in requests)
    raise ToolError(f"{len(requests)} {kind}s pending ({ids}); pass request_id")


@server.tool(annotations=WRITE)
def abort(workdir: Workdir = None) -> str:
    """Stop the implementer's current turn. The session stays open."""
    with _bridge(workdir) as client:
        return client.post("/abort", Ok).detail


@server.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
def finish(workdir: Workdir = None) -> str:
    """Stop the implementer for this project when the task is done. Evidence is kept."""
    with _bridge(workdir) as client:
        return client.post("/finish", Ok).detail


def main() -> None:
    # One INFO line per bridge request drowns the client's MCP log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server.run("stdio")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
