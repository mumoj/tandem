"""A deterministic stand-in for a model: an OpenAI-compatible server with a script.

OpenCode talks to it through its ordinary ``@ai-sdk/openai-compatible``
provider, so everything between the supervisor and the model is the real
thing: the real ``opencode serve``, its permission system, its event stream.
Only the model's choices are scripted.

A script maps a marker to a list of steps. Each request is answered from the
list whose marker appears in the most recent user message that has one (a
spec prompt carries ``TANDEM_SPEC_ID: S-1``, a directive ``TANDEM_DIRECTIVE_ID:
D-1``), at the index given by how many assistant messages follow that message.
Counting from the prompt rather than from the start of the session keeps the
script on track when a turn is aborted part way through.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] | Callable[[list[dict[str, Any]]], dict[str, Any]]
    say: str = ""


@dataclass(frozen=True)
class Reply:
    text: str


Step = ToolCall | Reply


@dataclass
class Script:
    steps: dict[str, list[Step]]
    """Steps per marker, e.g. ``{"TANDEM_SPEC_ID: S-1": [...]}``."""
    requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def next_step(self, messages: list[dict[str, Any]]) -> Step:
        for position in range(len(messages) - 1, -1, -1):
            if messages[position].get("role") != "user":
                continue
            text = _text(messages[position])
            for marker, steps in self.steps.items():
                if marker in text:
                    after = messages[position + 1 :]
                    index = sum(1 for message in after if message.get("role") == "assistant")
                    return steps[index] if index < len(steps) else Reply("Nothing left to do.")
        raise AssertionError("no scripted marker in the conversation")


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content  # pyright: ignore[reportUnknownVariableType]
            if isinstance(part, dict)
        )
    return ""


def _search(messages: list[dict[str, Any]], pattern: str) -> str:
    for message in messages:
        if match := re.search(pattern, _text(message)):
            return match.group(1)
    raise AssertionError(f"{pattern!r} not found in the conversation")


def result_path(messages: list[dict[str, Any]]) -> str:
    """The result file path tandem put in the spec prompt."""
    return _search(messages, r"writing JSON to (\S+?):")


def _chunk(delta: dict[str, Any], finish: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-scripted",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "scripted",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def build_app(script: Script) -> FastAPI:
    app = FastAPI()

    @app.get("/v1/models")
    async def models() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return JSONResponse({"object": "list", "data": [{"id": "scripted", "object": "model"}]})

    @app.post("/v1/chat/completions")
    async def complete(request: Request) -> StreamingResponse:  # pyright: ignore[reportUnusedFunction]
        body: dict[str, Any] = await request.json()
        script.requests.append(body)
        messages: list[dict[str, Any]] = body.get("messages", [])
        # Requests without tools are OpenCode's own housekeeping (titles).
        step = script.next_step(messages) if body.get("tools") else Reply("Scripted session")

        async def stream() -> AsyncIterator[str]:
            if isinstance(step, ToolCall):
                arguments = step.arguments
                if callable(arguments):
                    arguments = arguments(messages)
                if step.say:
                    yield _chunk({"role": "assistant", "content": step.say})
                yield _chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"call_{len(script.requests)}",
                                "type": "function",
                                "function": {"name": step.name, "arguments": json.dumps(arguments)},
                            }
                        ]
                    }
                )
                yield _chunk({}, "tool_calls")
            else:
                yield _chunk({"role": "assistant", "content": step.text})
                yield _chunk({}, "stop")
            usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
            final = {
                "id": "chatcmpl-scripted",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "scripted",
                "choices": [],
                "usage": usage,
            }
            yield f"data: {json.dumps(final)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
