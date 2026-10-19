"""The control bridge: a narrow, authenticated HTTP API over the supervisor.

The planner and the human on a phone use the same endpoints, through the same
``tandem`` client. Anything the planner can do, the human can do too, and every
call from either lands in the same journal.

It binds to loopback and deliberately does not proxy OpenCode. A dozen named
operations keep the trial's networking private and make every intervention an
explicit, auditable call.

``/ui`` is the one unauthenticated route: a static page holding no data. It
reads the token from its URL fragment, which browsers never send to a server,
and calls the same authenticated endpoints as everyone else.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.resources import files
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from tandem import __version__
from tandem.bridge.schemas import (
    AnswerIn,
    DiffOut,
    DirectiveIn,
    DirectiveOut,
    JournalOut,
    MessageOut,
    MessagesOut,
    Ok,
    OpenCodeOut,
    OverviewOut,
    PermissionIn,
    PromptIn,
    ResultOut,
    RetractIn,
    SpecIn,
    SpecOut,
    WaitOut,
)
from tandem.opencode.client import NotFoundError, OpenCodeError
from tandem.supervisor.state import SpecRecord
from tandem.supervisor.supervisor import (
    CostReport,
    Pending,
    StatusReport,
    Supervisor,
    SupervisorError,
)

MAX_BODY_BYTES = 64 * 1024

_bearer = HTTPBearer(auto_error=False, description="The token from the manifest.")


def get_supervisor(request: Request) -> Supervisor:
    supervisor: Supervisor | None = getattr(request.app.state, "supervisor", None)
    if supervisor is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "supervisor is starting")
    return supervisor


def require_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    expected: str = request.app.state.token
    given = credentials.credentials if credentials else ""
    # Constant-time comparison: the token is the only thing guarding the bridge.
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing or wrong bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


SupervisorDep = Annotated[Supervisor, Depends(get_supervisor)]


class BodyLimit:
    """Refuse oversized bodies before anything parses them.

    Plain ASGI rather than ``BaseHTTPMiddleware``, which wraps every request in
    an extra task and buffers what passes through it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            length = Headers(scope=scope).get("content-length")
            if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
                response = JSONResponse(
                    {"detail": f"request body over {MAX_BODY_BYTES} bytes"},
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _spec_out(supervisor: Supervisor, record: SpecRecord) -> SpecOut:
    return SpecOut(
        spec_id=record.spec_id,
        session_id=record.session_id,
        status=record.status,
        result_path=str(supervisor.result_path(record)),
    )


router = APIRouter(dependencies=[Depends(require_token)])


@router.get("/status")
async def read_status(supervisor: SupervisorDep) -> StatusReport:
    return supervisor.status()


@router.get("/overview")
async def read_overview(supervisor: SupervisorDep) -> OverviewOut:
    attention = supervisor.attention()
    return OverviewOut(
        status=supervisor.status(),
        attention=attention.reason if attention else None,
        attention_detail=attention.detail if attention else "",
        pending=await supervisor.pending(),
    )


@router.get("/journal")
def read_journal(  # a plain def: FastAPI runs it in its threadpool, off the event loop
    supervisor: SupervisorDep,
    after: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(gt=0, le=1000)] = 200,
) -> JournalOut:
    entries, offset = supervisor.journal.read_from(after, limit)
    return JournalOut(entries=entries, next=offset)


@router.get("/opencode")
async def read_opencode(request: Request) -> OpenCodeOut:
    """OpenCode's own web UI for this task, for watching the session live."""
    login: OpenCodeLogin | None = request.app.state.opencode
    return OpenCodeOut(url=login.url if login else None, password=login.password if login else None)


@router.get("/wait")
async def wait(
    supervisor: SupervisorDep,
    seconds: Annotated[float, Query(alias="timeout", gt=0, le=3600)] = 600,
) -> WaitOut:
    """Block until the implementer needs the planner, or ``timeout`` seconds pass."""
    attention = await supervisor.wait(seconds)
    return WaitOut(reason=attention.reason, detail=attention.detail, status=supervisor.status())


@router.get("/messages")
async def read_messages(
    supervisor: SupervisorDep, limit: Annotated[int, Query(gt=0, le=500)] = 20
) -> MessagesOut:
    messages = await supervisor.messages(limit)
    record = supervisor.state.current
    assert record is not None  # messages() already required one
    return MessagesOut(session_id=record.session_id, messages=[MessageOut.of(m) for m in messages])


@router.get("/pending")
async def read_pending(supervisor: SupervisorDep) -> Pending:
    return await supervisor.pending()


@router.get("/result")
async def read_result(supervisor: SupervisorDep) -> ResultOut:
    record = supervisor.state.current
    path = str(supervisor.result_path(record)) if record else None
    return ResultOut(path=path, result=supervisor.result())


@router.get("/diff")
async def read_diff(supervisor: SupervisorDep) -> DiffOut:
    return DiffOut(files=await supervisor.diff())


@router.get("/cost")
async def read_cost(supervisor: SupervisorDep) -> CostReport:
    return await supervisor.cost()


@router.post("/spec")
async def submit_spec(body: SpecIn, supervisor: SupervisorDep) -> SpecOut:
    """Start a spec in a fresh session. Idempotent for the same id and text."""
    record = await supervisor.submit_spec(
        body.spec_id, body.text, model=body.model, agent=body.agent
    )
    return _spec_out(supervisor, record)


@router.post("/directive")
async def send_directive(body: DirectiveIn, supervisor: SupervisorDep) -> DirectiveOut:
    directive = await supervisor.directive(body.spec_id, body.directive_id, body.action, body.text)
    return DirectiveOut(
        spec_id=body.spec_id,
        directive_id=directive.directive_id,
        action=directive.action,
        retracted=directive.retracted,
    )


@router.post("/retract")
async def retract(body: RetractIn, supervisor: SupervisorDep) -> DirectiveOut:
    directive = await supervisor.retract(body.spec_id, body.directive_id)
    return DirectiveOut(
        spec_id=body.spec_id,
        directive_id=directive.directive_id,
        action=directive.action,
        retracted=directive.retracted,
    )


@router.post("/prompt", status_code=status.HTTP_202_ACCEPTED)
async def prompt(body: PromptIn, supervisor: SupervisorDep) -> SpecOut:
    record = await supervisor.say(body.text)
    return _spec_out(supervisor, record)


@router.post("/permission")
async def reply_permission(body: PermissionIn, supervisor: SupervisorDep) -> Ok:
    await supervisor.reply_permission(body.id, body.reply, body.message)
    return Ok(detail=f"{body.id} {body.reply}")


@router.post("/answer")
async def answer(body: AnswerIn, supervisor: SupervisorDep) -> Ok:
    await supervisor.answer(body.id, None if body.reject else body.answers)
    return Ok(detail=f"{body.id} {'rejected' if body.reject else 'answered'}")


@router.post("/abort")
async def abort(supervisor: SupervisorDep) -> Ok:
    record = await supervisor.abort()
    return Ok(detail=f"aborted the turn on {record.spec_id}; the session stays open")


@router.post("/finish")
async def finish(request: Request) -> Ok:
    """Stop the supervisor. Evidence is captured on the way out."""
    on_finish: Callable[[], None] = request.app.state.on_finish
    on_finish()
    return Ok(detail="stopping")


@dataclass(frozen=True, slots=True)
class OpenCodeLogin:
    url: str
    password: str


def dashboard_headers() -> dict[str, str]:
    """For a page that holds no data and talks only to the server that sent it."""
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"
        ),
    }


def _dashboard() -> HTMLResponse:
    page = files("tandem.bridge").joinpath("dashboard.html").read_text(encoding="utf-8")
    return HTMLResponse(page, headers=dashboard_headers())


def _error(status_code: int) -> Callable[[Request, Exception], Awaitable[Response]]:
    async def handler(_request: Request, exc: Exception) -> Response:
        return JSONResponse({"detail": str(exc)}, status_code=status_code)

    return handler


def create_app(
    *,
    supervisor: Supervisor | None = None,
    token: str,
    on_finish: Callable[[], None] = lambda: None,
    opencode: OpenCodeLogin | None = None,
) -> FastAPI:
    app = FastAPI(
        title="tandem bridge",
        version=__version__,
        description="Drive a supervised OpenCode implementer. Every call needs the bearer token.",
    )
    app.state.supervisor = supervisor
    app.state.token = token
    app.state.on_finish = on_finish
    app.state.opencode = opencode
    app.add_middleware(BodyLimit)
    app.add_exception_handler(SupervisorError, _error(status.HTTP_409_CONFLICT))
    # Starlette walks the exception's MRO, so NotFoundError gets its own status even
    # though it is also an OpenCodeError.
    app.add_exception_handler(OpenCodeError, _error(status.HTTP_502_BAD_GATEWAY))
    app.add_exception_handler(NotFoundError, _error(status.HTTP_404_NOT_FOUND))
    app.include_router(router)
    app.add_api_route("/ui", _dashboard, methods=["GET"], include_in_schema=False)
    return app
