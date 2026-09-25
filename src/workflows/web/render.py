import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.responses import Response

from workflows.accounts.auth import CurrentUser, is_admin, verify_csrf
from workflows.accounts.ledger import grant_daily
from workflows.api.views import JobEventView, JobView
from workflows.db import Job, JobStatus, LedgerEntry, LedgerKind, LinkRequest, User, utcnow
from workflows.jobtypes.catalog import JobType, Step
from workflows.jobtypes.forms import FieldSpec
from workflows.settings import Settings
from workflows.state import AppServices, Db, Services

RING_RADIUS = 44
RING_CIRCUMFERENCE = round(2 * math.pi * RING_RADIUS, 1)
TOPUP_HINT = "not enough credits, top up in your wallet first"

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["ring_radius"] = RING_RADIUS
templates.env.globals["ring_circumference"] = RING_CIRCUMFERENCE


@dataclass(frozen=True)
class MeChip:
    username: str
    free_credits: int
    paid_credits: int
    admin: bool


def me_chip(settings: Settings, user: User) -> MeChip:
    return MeChip(user.username, user.free_credits, user.paid_credits, is_admin(user, settings))


@dataclass(frozen=True)
class StepRow:
    name: str
    weight: int
    state: Literal["todo", "now", "done"]
    pct: int
    label: str
    when: str


def format_seconds(seconds: int | float | None) -> str:
    if seconds is None:
        return ""
    total = max(0, round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _step_starts(events: list[JobEventView]) -> dict[str, datetime]:
    starts: dict[str, datetime] = {}
    for event in events:
        step_name = event.data.get("step")
        if event.kind == "step" and isinstance(step_name, str):
            starts.setdefault(step_name, event.created_at)
    return starts


def _step_durations(job: JobView, job_type: JobType, events: list[JobEventView]) -> dict[str, str]:
    starts = _step_starts(events)
    seen = [step.name for step in job_type.steps if step.name in starts]
    if not seen:
        return {}
    ends = [starts[name] for name in seen[1:]] + [job.finished_at or utcnow()]
    return {
        name: format_seconds((end - starts[name]).total_seconds())
        for name, end in zip(seen, ends, strict=True)
    }


def _todo_row(step: Step) -> StepRow:
    return StepRow(step.name, step.weight, "todo", 0, step.name, "")


def _done_row(step: Step, when: str) -> StepRow:
    return StepRow(step.name, step.weight, "done", 100, step.name, when)


def _now_row(step: Step, job: JobView, when: str) -> StepRow:
    done, total = job.progress.done, job.progress.total
    pct = round(100 * done / total) if done is not None and total else 0
    label = f"{step.name} · {done}/{total}" if total else step.name
    return StepRow(step.name, step.weight, "now", pct, label, when)


def step_rows(job: JobView, job_type: JobType, events: list[JobEventView]) -> list[StepRow]:
    durations = _step_durations(job, job_type, events)
    if job.status == JobStatus.SUCCEEDED:
        return [_done_row(step, durations.get(step.name, "")) for step in job_type.steps]
    if job.progress.step is None:
        return [_todo_row(step) for step in job_type.steps]
    rows = []
    reached = False
    for step in job_type.steps:
        when = durations.get(step.name, "")
        if step.name == job.progress.step:
            reached = True
            rows.append(_now_row(step, job, when))
        elif reached:
            rows.append(_todo_row(step))
        else:
            rows.append(_done_row(step, when))
    return rows


def ring_offset(fraction: float) -> float:
    return round(RING_CIRCUMFERENCE * (1 - min(max(fraction, 0.0), 1.0)), 1)


@dataclass(frozen=True)
class LogLine:
    time: str
    text: str


def _log_text(event: JobEventView) -> str:
    match event.kind:
        case "step":
            done, total = event.data.get("done"), event.data.get("total")
            suffix = f" {done}/{total}" if total else ""
            return f"{event.data.get('step')}{suffix}"
        case "log":
            return str(event.data.get("message", ""))
        case "result":
            return "result received"
        case "error":
            return f"error: {event.data.get('message', '')}"


def log_lines(events: list[JobEventView]) -> list[LogLine]:
    return [LogLine(event.created_at.strftime("%H:%M:%S"), _log_text(event)) for event in events]


def is_playable_url(url: str) -> bool:
    return url.startswith(("http://", "https://"))


def is_listenable(mime: str) -> bool:
    return mime.startswith("audio/") and mime != "audio/midi"


STATUS_LABELS = {
    JobStatus.AWAITING_CONFIRMATION: "waiting for confirmation",
    JobStatus.QUEUED: "in line",
    JobStatus.RUNNING: "running",
    JobStatus.SUCCEEDED: "done",
    JobStatus.FAILED: "failed",
    JobStatus.CANCELLED: "cancelled",
}

LEDGER_LABELS = {
    LedgerKind.FREE_GRANT: "daily free credits",
    LedgerKind.TOPUP: "top-up",
    LedgerKind.RESERVE: "paid for",
    LedgerKind.CAPTURE: "charged for",
    LedgerKind.REFUND: "refund for",
    LedgerKind.ADMIN_ADJUST: "adjusted by an admin",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(JobStatus(status), status)


def ledger_label(kind: str) -> str:
    return LEDGER_LABELS.get(LedgerKind(kind), kind)


MINUTE_SECONDS = 60
HOUR_SECONDS = 3600
DAY_SECONDS = 86400
WEEK_SECONDS = 7 * DAY_SECONDS
MONTH_SECONDS = 30 * DAY_SECONDS


def short_time(value: datetime | None) -> str:
    return value.strftime("%b %d %H:%M") if value else "never"


def _ago(count: int, unit: str) -> str:
    return f"{count} {unit} ago" if count == 1 else f"{count} {unit}s ago"


AGE_WORDS: tuple[tuple[int, Callable[[int], str]], ...] = (
    (MINUTE_SECONDS, lambda _: "just now"),
    (HOUR_SECONDS, lambda seconds: f"{seconds // MINUTE_SECONDS} min ago"),
    (DAY_SECONDS, lambda seconds: _ago(seconds // HOUR_SECONDS, "hour")),
    (2 * DAY_SECONDS, lambda _: "yesterday"),
    (WEEK_SECONDS, lambda seconds: _ago(seconds // DAY_SECONDS, "day")),
    (MONTH_SECONDS, lambda seconds: _ago(seconds // WEEK_SECONDS, "week")),
)


def relative_time(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return "never"
    now = now or utcnow()
    seconds = int((now - value).total_seconds())
    for limit, words in AGE_WORDS:
        if seconds < limit:
            return words(seconds)
    return value.strftime("%b %-d" if value.year == now.year else "%b %-d, %Y")


def credits(amount: int) -> str:
    return f"{amount:,} credit" if amount == 1 else f"{amount:,} credits"


templates.env.tests["playable"] = is_playable_url
templates.env.tests["listenable"] = is_listenable
templates.env.filters["credits"] = credits
templates.env.filters["status_label"] = status_label
templates.env.filters["ledger_label"] = ledger_label
templates.env.filters["short_time"] = short_time
templates.env.filters["relative_time"] = relative_time


@dataclass(frozen=True)
class RunningPanel:
    job: JobView
    job_type: JobType
    steps: list[StepRow]
    ring_offset: float
    eta_label: str


def running_panel(job: JobView, job_type: JobType, events: list[JobEventView]) -> RunningPanel:
    return RunningPanel(
        job=job,
        job_type=job_type,
        steps=step_rows(job, job_type, events),
        ring_offset=ring_offset(job.progress.fraction),
        eta_label=format_seconds(job.eta_seconds),
    )


type TemplateValue = (
    str
    | int
    | BaseModel
    | JobType
    | RunningPanel
    | MeChip
    | Job
    | LinkRequest
    | Sequence[BaseModel | FieldSpec | LogLine | LedgerEntry | str]
    | None
)


@dataclass(frozen=True)
class Page:
    request: Request
    session: Session
    services: Services
    user: User | None


async def get_page(request: Request, session: Db, services: AppServices, user: CurrentUser) -> Page:
    if user is not None:
        grant_daily(session, user)
        session.commit()
    return Page(request, session, services, user)


PageCtx = Annotated[Page, Depends(get_page)]


def render(
    page: Page, template: str, context: Mapping[str, TemplateValue], status_code: int = 200
) -> Response:
    me = me_chip(page.services.settings, page.user) if page.user else None
    full: dict[str, TemplateValue] = {**context, "me": me}
    return templates.TemplateResponse(page.request, template, full, status_code=status_code)


def login_redirect(next_path: str) -> RedirectResponse:
    return RedirectResponse(f"/login?next={next_path}", status_code=303)


async def verified_form(request: Request) -> FormData:
    form = await request.form()
    csrf = form.get("csrf_token")
    verify_csrf(request, csrf if isinstance(csrf, str) else "")
    return form


def form_str(form: FormData, name: str) -> str:
    raw = form.get(name)
    return raw if isinstance(raw, str) else ""
