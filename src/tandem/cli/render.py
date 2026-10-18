"""Terse, phone-width renderings of bridge responses.

Every command also takes ``--json`` for the raw response, which is what a
planner should parse. These are for a human on a small screen.
"""

from __future__ import annotations

from tandem.bridge.schemas import DiffOut, MessagesOut
from tandem.supervisor.supervisor import CostReport, Pending, StatusReport


def status(report: StatusReport) -> list[str]:
    activity = report.activity
    if report.spec_id is None:
        return ["no spec yet; start one with `tandem spec <id> <text>`"]
    lines = [
        f"spec      {report.spec_id}  [{report.spec_status}]",
        f"session   {report.session_id}",
        f"activity  {activity.activity}  tool {activity.seconds_since_tool}s ago, "
        f"text {activity.seconds_since_text}s ago",
    ]
    if activity.running_tools:
        lines.append(f"running   {', '.join(activity.running_tools)}")
    if activity.pending:
        lines.append(f"pending   {', '.join(activity.pending)}  (see `tandem pending`)")
    if activity.retry:
        lines.append(f"retrying  {activity.retry}")
    if activity.error:
        lines.append(f"error     {activity.error}")
    if report.active_directives:
        lines.append(f"directives {', '.join(report.active_directives)}")
    if report.result:
        lines.append(f"result    {report.result.verdict}: {clip(report.result.summary, 200)}")
    return lines


def pending(data: Pending) -> list[str]:
    lines: list[str] = []
    for permission in data.permissions:
        patterns = ",".join(permission.patterns)
        lines.append(f"PERM {permission.id}  {permission.permission}  {clip(patterns, 80)}")
    for request in data.questions:
        for question in request.questions:
            lines.append(f"QUES {request.id}  {clip(question.question, 120)}")
            if question.options:
                lines.append(f"     options: {' | '.join(o.label for o in question.options)}")
    return lines or ["nothing pending"]


def messages(data: MessagesOut, count: int) -> list[str]:
    lines: list[str] = []
    for message in data.messages[-count:]:
        tools = f"  tools: {', '.join(message.tools)}" if message.tools else ""
        lines.append(f"--- {message.role}{tools}")
        if message.text:
            lines.append(tail(message.text, 1200))
    return lines or ["no messages yet"]


def cost(report: CostReport) -> list[str]:
    lines = [
        f"{spec.spec_id:<24} ${spec.cost:.4f}  in {spec.tokens.input:.0f}  "
        f"out {spec.tokens.output:.0f}  cache {spec.tokens.cache.read:.0f}"
        for spec in report.specs
    ]
    total = report.total_tokens
    lines.append(
        f"{'total':<24} ${report.total_cost:.4f}  in {total.input:.0f}  out {total.output:.0f}"
    )
    return lines


def diff(data: DiffOut) -> list[str]:
    if not data.files:
        return ["no changes"]
    return [
        f"{(item.status or '?')[:1].upper()} {item.file}  "
        f"+{item.additions:.0f} -{item.deletions:.0f}"
        for item in data.files
    ]


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tail(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]
