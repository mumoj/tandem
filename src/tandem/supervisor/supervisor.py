"""The supervisor: one immutable spec at a time, each in a fresh OpenCode session.

The planner talks to this class through the bridge, and the same calls serve a
human on a phone. Three rules shape it, each learned from a failure:

* **A Spec ID is immutable and gets its own session.** Resubmitting the same ID
  with the same text is a no-op, so a retrying planner cannot duplicate work;
  the same ID with different text is refused, because a spec that changes
  under a running session is how two halves of a task end up built against
  different requirements.
* **A directive belongs to one spec.** The first version kept directives in a
  file that outlived the spec they were written for. A leftover CORRECT read as
  already applied ended a later turn before the implementer had read its new
  spec. Here a directive names its spec and is refused if that spec is no
  longer the active one.
* **The supervisor checks; it does not trust.** A result counts only if it was
  written after the latest prompt, and protected files are hashed while the
  spec runs rather than relying on the prompt asking nicely.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from tandem.config import TaskPaths
from tandem.notify import Alert, Notifier
from tandem.opencode.client import NotFoundError, OpenCodeAPI, OpenCodeError
from tandem.opencode.events import Event
from tandem.opencode.models import (
    FileDiff,
    Message,
    ModelRef,
    PermissionReply,
    PermissionRequest,
    QuestionRequest,
    Session,
    TokenUsage,
)
from tandem.supervisor.activity import Activity, ActivityReport, ActivityTracker
from tandem.supervisor.journal import Journal
from tandem.supervisor.prompts import (
    directive_marker,
    directive_prompt,
    retraction_prompt,
    spec_marker,
    spec_prompt,
)
from tandem.supervisor.protected import ProtectedFiles
from tandem.supervisor.results import ImplementerResult, read_result
from tandem.supervisor.state import (
    DirectiveAction,
    DirectiveRecord,
    SpecRecord,
    SpecStatus,
    StateStore,
    write_private,
)


class SupervisorError(RuntimeError):
    """A request that conflicts with the supervisor's current state."""


class NoActiveSpecError(SupervisorError):
    def __init__(self) -> None:
        super().__init__("no active spec; submit one with `tandem spec`")


class ConflictError(SupervisorError):
    pass


class WaitReason(StrEnum):
    """Why ``wait`` returned. Each one asks the planner to do something."""

    RESULT = "result"
    TURN_ENDED = "turn_ended"
    BLOCKED = "blocked"
    REASONING_STALL = "reasoning_stall"
    HUNG = "hung"
    ERROR = "error"
    PROTECTED_VIOLATION = "protected_violation"
    NO_SPEC = "no_spec"
    STOPPED = "stopped"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class Attention:
    reason: WaitReason
    detail: str = ""


class Pending(BaseModel):
    permissions: list[PermissionRequest]
    questions: list[QuestionRequest]


class SpecCost(BaseModel):
    spec_id: str
    session_id: str
    cost: float
    tokens: TokenUsage


class CostReport(BaseModel):
    specs: list[SpecCost]
    total_cost: float
    total_tokens: TokenUsage


class StatusReport(BaseModel):
    task_dir: str
    spec_id: str | None
    session_id: str | None
    spec_status: SpecStatus | None
    activity: ActivityReport
    active_directives: list[str]
    result: ImplementerResult | None


_TURN_OVER = frozenset({"session.idle", "session.error"})


def digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()


def safe_name(spec_id: str) -> str:
    """A filesystem-safe, collision-free name for a spec's files."""
    stem = re.sub(r"[^A-Za-z0-9._+-]+", "-", spec_id).strip("-.")[:72] or "spec"
    return f"{stem}-{hashlib.sha256(spec_id.encode()).hexdigest()[:10]}"


class Supervisor:
    def __init__(
        self,
        *,
        api: OpenCodeAPI,
        paths: TaskPaths,
        task_dir: Path,
        store: StateStore,
        journal: Journal,
        notifier: Notifier,
        tracker: ActivityTracker,
        protected: ProtectedFiles,
        model: ModelRef | None = None,
        abort_settle: float = 5.0,
    ) -> None:
        self.api = api
        self.paths = paths
        self.task_dir = task_dir
        self.store = store
        self.journal = journal
        self.notifier = notifier
        self.tracker = tracker
        self.protected = protected
        self.model = model
        self.abort_settle = abort_settle
        self.state = store.load()
        # Mutations of specs and directives are serialized: two planner retries
        # racing through submit_spec must not create two sessions.
        self._mutex = asyncio.Lock()
        self._changed = asyncio.Condition()
        self._last_activity = Activity.IDLE
        self._resync_needed = False
        self.closing = False

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Resume whatever the state file says was running."""
        record = self.state.current
        if record is not None:
            async with self._mutex:
                await self._resume(record)
        self.journal.append(
            "supervisor_started",
            spec_id=self.state.current_spec_id,
            session_id=record.session_id if record else None,
        )

    async def _resume(self, record: SpecRecord) -> None:
        running = record.status in (SpecStatus.ACTIVE, SpecStatus.PROMPT_PENDING)
        try:
            await self.api.get_session(record.session_id)
        except NotFoundError:
            # The server lost the session (its data directory was reset, say).
            # Work still in flight is redelivered to a new session: the spec,
            # then its live corrections. Finished work is left for the planner.
            lost = record.session_id
            record.lost_sessions.append(lost)
            if running:
                record.session_id = (await self._create_session(record.spec_id)).id
                record.status = SpecStatus.PROMPT_PENDING
                for directive in record.directives.values():
                    directive.delivered = False
            self._save()
            self.journal.append(
                "session_missing",
                spec_id=record.spec_id,
                lost_session_id=lost,
                session_id=record.session_id,
            )
        self.tracker.track(record.session_id)
        if record.status is SpecStatus.PROMPT_PENDING:
            await self._deliver_spec(record, recovering=True)
        for directive in record.directives.values():
            if not directive.delivered and not directive.retracted:
                await self._redeliver_directive(record, directive)
        await self.resync()

    async def begin_shutdown(self) -> None:
        """Release every ``wait`` so a long poll cannot hold the process open."""
        self.closing = True
        await self._notify_waiters()

    async def shutdown(self) -> None:
        record = self.state.current
        if record is not None:
            await self._capture_evidence(record)
        self.journal.append("supervisor_stopped", spec_id=self.state.current_spec_id)

    # ---- specs -------------------------------------------------------------

    async def submit_spec(self, spec_id: str, text: str) -> SpecRecord:
        spec_digest = digest(text)
        async with self._mutex:
            existing = self.state.specs.get(spec_id)
            if existing is not None:
                if existing.digest != spec_digest:
                    raise ConflictError(
                        f"Spec ID {spec_id} is immutable and was already submitted with "
                        "different text; send the change under a new Spec ID"
                    )
                if spec_id != self.state.current_spec_id:
                    raise ConflictError(
                        f"Spec ID {spec_id} was already used and superseded; use a new Spec ID"
                    )
                if existing.status is SpecStatus.PROMPT_PENDING:
                    await self._deliver_spec(existing, recovering=True)
                return existing

            previous = self.state.current
            if previous is not None:
                # Otherwise the tampered file would become the next baseline,
                # and the violation would be quietly accepted.
                self._require_protected_intact(previous)
                await self._retire(previous)
            session = await self._create_session(spec_id)
            record = SpecRecord(
                spec_id=spec_id,
                digest=spec_digest,
                text=text,
                session_id=session.id,
                protected=self.protected.snapshot(),
            )
            self.state.specs[spec_id] = record
            self.state.current_spec_id = spec_id
            self._save()
            self.journal.append("session_created", spec_id=spec_id, session_id=session.id)
            self.tracker.track(session.id)
            self._last_activity = Activity.IDLE
            await self._deliver_spec(record, recovering=False)
        await self._notify_waiters()
        return record

    async def _create_session(self, spec_id: str) -> Session:
        title = f"tandem {self.task_dir.name} [{spec_id}]"
        return await self.api.create_session(title, self.protected.rules())

    async def _retire(self, record: SpecRecord) -> None:
        with contextlib.suppress(OpenCodeError):
            await self.api.abort(record.session_id)
        await self._capture_evidence(record)
        if record.status in (SpecStatus.ACTIVE, SpecStatus.PROMPT_PENDING):
            record.status = SpecStatus.SUPERSEDED
        self._save()
        self.journal.append("spec_retired", spec_id=record.spec_id, status=record.status)

    async def _deliver_spec(self, record: SpecRecord, *, recovering: bool) -> None:
        marker = spec_marker(record.spec_id)
        already = recovering and await self._session_contains(record.session_id, marker)
        if not already:
            prompt = spec_prompt(
                record.spec_id, record.text, self.result_path(record), self.protected.patterns
            )
            await self._prompt(record, prompt)
        record.status = SpecStatus.ACTIVE
        self._save()
        self.journal.append(
            "spec_delivered",
            spec_id=record.spec_id,
            session_id=record.session_id,
            recovered=already,
        )

    # ---- directives --------------------------------------------------------

    async def directive(
        self, spec_id: str, directive_id: str, action: DirectiveAction, text: str
    ) -> DirectiveRecord:
        directive_digest = digest(f"{action.value}\n{text}")
        async with self._mutex:
            record = self._require_spec(spec_id)
            existing = record.directives.get(directive_id)
            if existing is not None:
                if existing.digest != directive_digest:
                    raise ConflictError(
                        f"directive {directive_id} already exists with other content"
                    )
                if existing.delivered:
                    return existing
            self._require_protected_intact(record)
            if action is DirectiveAction.STOP_AND_WAIT:
                await self._abort_and_settle(record)
            directive = existing or DirectiveRecord(
                directive_id=directive_id, action=action, text=text, digest=directive_digest
            )
            record.directives[directive_id] = directive
            self._save()
            await self._prompt(record, directive_prompt(directive_id, action, text))
            directive.delivered = True
            record.status = SpecStatus.ACTIVE
            self._save()
            self.journal.append(
                "directive_delivered",
                spec_id=spec_id,
                directive_id=directive_id,
                directive_action=action,
            )
        await self._notify_waiters()
        return directive

    async def _redeliver_directive(self, record: SpecRecord, directive: DirectiveRecord) -> None:
        """Finish a delivery a crash interrupted. Only reached from ``_resume``."""
        marker = directive_marker(directive.directive_id)
        already = await self._session_contains(record.session_id, marker)
        if not already:
            prompt = directive_prompt(directive.directive_id, directive.action, directive.text)
            await self._prompt(record, prompt)
        directive.delivered = True
        self._save()
        self.journal.append(
            "directive_delivered",
            spec_id=record.spec_id,
            directive_id=directive.directive_id,
            directive_action=directive.action,
            recovered=already,
        )

    async def retract(self, spec_id: str, directive_id: str) -> DirectiveRecord:
        async with self._mutex:
            record = self._require_spec(spec_id)
            directive = record.directives.get(directive_id)
            if directive is None:
                raise ConflictError(f"spec {spec_id} has no directive {directive_id}")
            if directive.retracted:
                return directive
            directive.retracted = True
            self._save()
            await self._prompt(record, retraction_prompt([directive_id]))
            self.journal.append("directive_retracted", spec_id=spec_id, directive_id=directive_id)
        await self._notify_waiters()
        return directive

    # ---- free-form control -------------------------------------------------

    async def say(self, text: str) -> SpecRecord:
        async with self._mutex:
            record = self._require_current()
            self._require_protected_intact(record)
            await self._prompt(record, text)
            record.status = SpecStatus.ACTIVE
            self._save()
            self.journal.append("prompt", spec_id=record.spec_id, characters=len(text))
        await self._notify_waiters()
        return record

    async def abort(self) -> SpecRecord:
        record = self._require_current()
        await self._abort_and_settle(record)
        self.journal.append("abort", spec_id=record.spec_id, session_id=record.session_id)
        await self._notify_waiters()
        return record

    async def _abort_and_settle(self, record: SpecRecord) -> None:
        """Abort the running turn and wait until its end has been observed.

        An abort's own ``session.idle`` arrives on the stream a moment later.
        Sending the next prompt before it lands would let that stale idle end
        the new turn as far as ``wait`` can tell, and the planner would review
        a turn that has barely started.
        """
        was_busy = self.tracker.busy
        await self.api.abort(record.session_id)
        if was_busy:

            async def ended() -> None:
                async with self._changed:
                    await self._changed.wait_for(lambda: not self.tracker.busy)

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ended(), self.abort_settle)
        self.tracker.turn_ended()

    async def pending(self) -> Pending:
        permissions, questions = await asyncio.gather(self.api.permissions(), self.api.questions())
        return Pending(permissions=permissions, questions=questions)

    async def reply_permission(
        self, request_id: str, reply: PermissionReply, message: str | None
    ) -> None:
        await self.api.reply_permission(request_id, reply, message)
        self.tracker.pending.discard(request_id)
        self.journal.append(
            "permission", request_id=request_id, reply=reply, spec_id=self.state.current_spec_id
        )
        await self._notify_waiters()

    async def answer(self, request_id: str, answers: list[list[str]] | None) -> None:
        if answers is None:
            await self.api.reject_question(request_id)
        else:
            await self.api.reply_question(request_id, answers)
        self.tracker.pending.discard(request_id)
        self.journal.append(
            "question",
            request_id=request_id,
            disposition="reject" if answers is None else "reply",
            spec_id=self.state.current_spec_id,
        )
        await self._notify_waiters()

    # ---- reads -------------------------------------------------------------

    async def messages(self, limit: int | None = None) -> list[Message]:
        return await self.api.messages(self._require_current().session_id, limit)

    async def diff(self) -> list[FileDiff]:
        return await self.api.diff(self._require_current().session_id)

    async def cost(self) -> CostReport:
        """Spend per spec, read from OpenCode's own per-session totals."""
        records = list(self.state.specs.values())

        async def fetch(record: SpecRecord) -> SpecCost | None:
            try:
                session = await self.api.get_session(record.session_id)
            except NotFoundError:
                return None
            return SpecCost(
                spec_id=record.spec_id,
                session_id=record.session_id,
                cost=session.cost,
                tokens=session.tokens or TokenUsage(),
            )

        specs = [c for c in await asyncio.gather(*map(fetch, records)) if c is not None]
        total = TokenUsage()
        for spec in specs:
            total.input += spec.tokens.input
            total.output += spec.tokens.output
            total.reasoning += spec.tokens.reasoning
            total.cache.read += spec.tokens.cache.read
            total.cache.write += spec.tokens.cache.write
        return CostReport(specs=specs, total_cost=sum(s.cost for s in specs), total_tokens=total)

    def result_path(self, record: SpecRecord) -> Path:
        return self.paths.results / f"{safe_name(record.spec_id)}.json"

    def result(self) -> ImplementerResult | None:
        record = self.state.current
        return read_result(self.result_path(record), record.spec_id) if record else None

    def status(self) -> StatusReport:
        record = self.state.current
        return StatusReport(
            task_dir=str(self.task_dir),
            spec_id=record.spec_id if record else None,
            session_id=record.session_id if record else None,
            spec_status=record.status if record else None,
            activity=self.tracker.describe(),
            active_directives=record.active_directives if record else [],
            result=self.result(),
        )

    # ---- waiting -----------------------------------------------------------

    def attention(self) -> Attention | None:
        """What the planner should act on now, or ``None`` while work goes on.

        Level-triggered on purpose: it reports the state, not the moment a state
        was entered, so a ``wait`` can never miss a wakeup that fired between
        two calls.
        """
        if self.closing:
            return Attention(WaitReason.STOPPED, "the supervisor is stopping")
        record = self.state.current
        if record is None:
            return Attention(WaitReason.NO_SPEC)
        if record.status is SpecStatus.PROTECTED_VIOLATION:
            return Attention(WaitReason.PROTECTED_VIOLATION, "protected files changed")
        if record.status is SpecStatus.PROMPT_PENDING:
            return Attention(WaitReason.ERROR, "spec not delivered; resubmit it")
        activity = self.tracker.classify()
        if record.status is SpecStatus.RESULT_WRITTEN and activity in (
            Activity.IDLE,
            Activity.ERROR,
        ):
            # A result is reported once the turn is over, not when the file
            # lands: the model may still be working after writing it, and a
            # review of a moving target is not a review. A turn that dies after
            # writing its result still reports the result; the outcome is safe.
            result = self.result()
            return Attention(WaitReason.RESULT, result.verdict if result else "")
        match activity:
            case Activity.BLOCKED:
                return Attention(WaitReason.BLOCKED, ", ".join(sorted(self.tracker.pending)))
            case Activity.ERROR:
                return Attention(WaitReason.ERROR, self.tracker.error or "")
            case Activity.REASONING_STALL:
                return Attention(WaitReason.REASONING_STALL, self._idle_detail())
            case Activity.HUNG:
                return Attention(WaitReason.HUNG, self._idle_detail())
            case Activity.IDLE:
                return Attention(WaitReason.TURN_ENDED, "turn ended without a result file")
            case Activity.WORKING:
                return None

    async def wait(self, seconds: float) -> Attention:
        async def settled() -> Attention:
            async with self._changed:
                await self._changed.wait_for(lambda: self.attention() is not None)
                attention = self.attention()
                assert attention is not None
                return attention

        try:
            return await asyncio.wait_for(settled(), seconds)
        except TimeoutError:
            return Attention(WaitReason.TIMEOUT, f"still working after {seconds:.0f}s")

    def _idle_detail(self) -> str:
        info = self.tracker.describe()
        return f"no tool for {info.seconds_since_tool}s, no text for {info.seconds_since_text}s"

    # ---- event stream ------------------------------------------------------

    async def handle_event(self, event: Event) -> None:
        """Fold one stream event into the supervisor's view."""
        if event.type == "server.connected":
            # A (re)connect means replies may have been missed while away.
            self._resync_needed = True
        self.tracker.observe(event)
        record = self.state.current
        mine = record is not None and event.session_id == record.session_id
        if record is not None and mine:
            alert = Alert.from_event(event)
            if alert is not None:
                self.journal.append(
                    "stream_event",
                    stream_type=event.type,
                    summary=alert.summary,
                    spec_id=record.spec_id,
                    session_id=record.session_id,
                )
                self.notifier.escalate(alert)
            elif event.type in _TURN_OVER:
                self.journal.append(
                    "stream_event",
                    stream_type=event.type,
                    spec_id=record.spec_id,
                    error=event.error_name,
                )
        if (mine and event.type in _TURN_OVER) or event.type == "file.edited":
            # Check now rather than on the next tick. A turn that wrote its
            # result, or tampered with a protected file, just before ending
            # would otherwise be reported as merely ended.
            await self._check_current()
        await self._notify_waiters()

    async def resync(self) -> None:
        """Rebuild what the event stream may have missed from the server's own view."""
        self._resync_needed = False
        record = self.state.current
        if record is None:
            return
        pending, statuses = await asyncio.gather(self.pending(), self.api.session_status())
        mine = {p.id for p in pending.permissions if p.session_id == record.session_id}
        mine |= {q.id for q in pending.questions if q.session_id == record.session_id}
        self.tracker.sync_pending(mine)
        status = statuses.get(record.session_id)
        self.tracker.sync_busy(status is not None and status.type in ("busy", "retry"))
        await self._notify_waiters()

    # ---- the monitor ---------------------------------------------------------

    async def tick(self) -> None:
        """One pass of the checks that time, not events, can trigger."""
        if self._resync_needed:
            try:
                await self.resync()
            except OpenCodeError as exc:
                self.journal.append("resync_failed", error=str(exc))
        await self._check_current()
        self._track_activity()
        await self._notify_waiters()

    async def _check_current(self) -> None:
        """Protected files first, then the result file, for the active spec.

        Each transition updates the status before its first ``await``, so a
        tick and an event racing through here act on it once.
        """
        record = self.state.current
        if record is None or record.status is not SpecStatus.ACTIVE:
            return
        changed = self.protected.changes(record.protected)
        if changed:
            await self._protected_violation(record, changed)
        elif (result := self._fresh_result(record)) is not None:
            record.status = SpecStatus.RESULT_WRITTEN
            self._save()
            self.journal.append(
                "result_observed",
                spec_id=record.spec_id,
                verdict=result.verdict,
                summary=result.summary[:200],
            )
            await self._capture_evidence(record)

    async def monitor(self, interval: float) -> None:
        while True:
            try:
                await self.tick()
            except OpenCodeError as exc:
                self.journal.append("monitor_error", error=str(exc))
            await asyncio.sleep(interval)

    def _fresh_result(self, record: SpecRecord) -> ImplementerResult | None:
        path = self.result_path(record)
        try:
            written = path.stat().st_mtime
        except OSError:
            return None
        if written < record.last_prompt_at:
            return None
        return read_result(path, record.spec_id)

    def _track_activity(self) -> None:
        activity = self.tracker.classify()
        if activity is self._last_activity:
            return
        previous, self._last_activity = self._last_activity, activity
        self.journal.append(
            "activity",
            previous=previous,
            current=activity,
            spec_id=self.state.current_spec_id,
        )
        if activity in (Activity.REASONING_STALL, Activity.HUNG):
            label = activity.value.replace("_", " ")
            spec = self.state.current_spec_id
            self.notifier.escalate(Alert(activity.value, f"{spec}: {label}, {self._idle_detail()}"))

    async def _protected_violation(self, record: SpecRecord, changed: list[str]) -> None:
        record.status = SpecStatus.PROTECTED_VIOLATION
        self._save()
        self.journal.append("protected_violation", spec_id=record.spec_id, files=changed)
        self.notifier.escalate(
            Alert(
                "protected_violation",
                f"{record.spec_id}: implementer changed {', '.join(changed)}; session aborted. "
                "Restore the files, then send a directive.",
            )
        )
        with contextlib.suppress(OpenCodeError):
            await self.api.abort(record.session_id)

    # ---- helpers -----------------------------------------------------------

    async def _prompt(self, record: SpecRecord, text: str) -> None:
        self.tracker.turn_started()
        record.last_prompt_at = time.time()
        try:
            await self.api.prompt(record.session_id, text, self.model)
        except BaseException:
            self.tracker.turn_ended()
            raise

    async def _session_contains(self, session_id: str, marker: str) -> bool:
        messages = await self.api.messages(session_id)
        return any(marker in message.text() for message in messages)

    async def _capture_evidence(self, record: SpecRecord) -> None:
        """Persist the session's final messages and totals next to the journal."""
        try:
            session, messages = await asyncio.gather(
                self.api.get_session(record.session_id), self.api.messages(record.session_id)
            )
        except OpenCodeError as exc:
            self.journal.append("evidence_failed", spec_id=record.spec_id, error=str(exc))
            return
        evidence = {
            "spec_id": record.spec_id,
            "session": session.model_dump(mode="json", by_alias=True),
            "messages": [m.model_dump(mode="json", by_alias=True) for m in messages],
        }
        target = self.paths.evidence / safe_name(record.spec_id) / "session.json"
        write_private(target, json.dumps(evidence, indent=2) + "\n")

    def _require_current(self) -> SpecRecord:
        record = self.state.current
        if record is None:
            raise NoActiveSpecError
        return record

    def _require_spec(self, spec_id: str) -> SpecRecord:
        record = self._require_current()
        if record.spec_id != spec_id:
            raise ConflictError(
                f"this targets spec {spec_id}, but the active spec is {record.spec_id}; "
                "a directive cannot outlive its spec"
            )
        return record

    def _require_protected_intact(self, record: SpecRecord) -> None:
        if record.status is not SpecStatus.PROTECTED_VIOLATION:
            return
        changed = self.protected.changes(record.protected)
        if changed:
            raise ConflictError(
                f"protected files still differ: {', '.join(changed)}; restore them first"
            )

    def _save(self) -> None:
        self.store.save(self.state)

    async def _notify_waiters(self) -> None:
        async with self._changed:
            self._changed.notify_all()
