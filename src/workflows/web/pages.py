from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from fastapi.responses import RedirectResponse
from pydantic import JsonValue
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.responses import Response

from workflows.accounts.account import MAX_TOPUP_BEANS
from workflows.accounts.account import TopupRequest
from workflows.accounts.account import ledger_entries
from workflows.accounts.account import linked_identities
from workflows.accounts.account import topup_url
from workflows.accounts.account import unlink_identity
from workflows.accounts.auth import csrf_token
from workflows.accounts.auth import require_admin
from workflows.accounts.ledger import InsufficientCreditsError
from workflows.api.admin import AdjustCreditsRequest
from workflows.api.admin import adjust_credits
from workflows.api.admin import cancel_job
from workflows.api.admin import health
from workflows.api.admin import remove_job_files
from workflows.api.admin import set_paused
from workflows.api.admin import user_or_404
from workflows.api.routes import QuoteRequest
from workflows.api.routes import get_job_type
from workflows.api.routes import price_request
from workflows.api.views import finished_job_views
from workflows.api.views import job_detail_view
from workflows.api.views import job_views
from workflows.api.views import queue_view
from workflows.api.views import quote_view
from workflows.api.views import type_view
from workflows.db import Job
from workflows.db import JobStatus
from workflows.db import JsonObject
from workflows.db import User
from workflows.jobtypes.catalog import JobType
from workflows.jobtypes.probe import ProbeError
from workflows.runs.jobs import JobError
from workflows.runs.jobs import announce
from workflows.runs.jobs import create_job
from workflows.runs.jobs import enqueue
from workflows.settings import CREDITS_PER_BEAN
from workflows.settings import FREE_DAILY_CREDITS
from workflows.state import Services
from workflows.web.render import TOPUP_HINT
from workflows.web.render import Page
from workflows.web.render import PageCtx
from workflows.web.render import TemplateValue
from workflows.web.render import form_str
from workflows.web.render import log_lines
from workflows.web.render import login_redirect
from workflows.web.render import render
from workflows.web.render import running_panel
from workflows.web.render import verified_form

type Context = dict[str, TemplateValue]

RESERVED_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)
RECENT_RESULTS = 4
USER_JOB_LIMIT = 50
RUNS_PAGE = 30
ADMIN_RECENT_JOBS = 50
ADMIN_JOB_FETCH = 200
PROBE_FAILED = "check the link, we could not read it"
FORM_INVALID = "check the form for mistakes"

router = APIRouter()


def params_from_form(form: FormData, job_type: JobType) -> JsonObject:
    params: JsonObject = {}
    for spec in job_type.form.fields:
        raw = form.get(spec.name)
        if spec.kind == "checkbox":
            params[spec.name] = spec.name in form
        elif isinstance(raw, str) and raw != "":
            params[spec.name] = raw
    return params


def _queue_context(session: Session, services: Services) -> Context:
    queue = queue_view(session, services)
    running = (
        running_panel(queue.running, services.catalog.find(queue.running.type), [])
        if queue.running
        else None
    )
    types = [type_view(job_type) for job_type in services.catalog.all()]
    recent = [
        job
        for job in job_views(session, services, None, 20)
        if job.status == JobStatus.SUCCEEDED
        and job.removed_at is None
        and services.catalog.get(job.type) is not None
    ][:RECENT_RESULTS]
    return {"queue": queue, "running": running, "types": types, "recent": recent}


@router.get("/")
async def home(page: PageCtx) -> Response:
    context = _queue_context(page.session, page.services)
    return render(page, "home.html", {**context, "active": "home"})


@router.get("/partials/queue")
async def partial_queue(page: PageCtx) -> Response:
    return render(page, "_queue_panel.html", _queue_context(page.session, page.services))


def _submit_context(
    page: Page, job_type: JobType, error: str | None = None, prefill: JsonObject | None = None
) -> Context:
    fields = list(job_type.form.fields)
    if prefill:
        fields = [
            replace(spec, default=prefill[spec.name]) if spec.name in prefill else spec
            for spec in fields
        ]
    return {
        "job_type": type_view(job_type),
        "fields": fields,
        "quote": None,
        "prefilled": bool(prefill),
        "error": error,
        "csrf_token": csrf_token(page.request),
    }


def _resubmit_page(
    page: Page, job_type: JobType, error: str, typed: JsonObject, status_code: int
) -> Response:
    context = _submit_context(page, job_type, error, prefill=typed)
    return render(page, "submit.html", context, status_code)


@router.get("/submit/{type_name}")
async def submit_page(
    type_name: str, page: PageCtx, from_job: Annotated[int | None, Query(alias="from")] = None
) -> Response:
    job_type = get_job_type(page.services, type_name)
    source = page.session.get(Job, from_job) if from_job is not None else None
    prefill = source.params if source is not None and source.type == type_name else None
    return render(page, "submit.html", _submit_context(page, job_type, prefill=prefill))


async def _price_form(page: Page, job_type: JobType, form: FormData) -> Context:
    request = QuoteRequest(type=job_type.name, params=params_from_form(form, job_type))
    try:
        _, _, priced = await price_request(page.services, request)
    except ProbeError:
        return {"error": PROBE_FAILED}
    return {"quote": quote_view(priced)}


@router.post("/submit/{type_name}/quote")
async def submit_quote(type_name: str, page: PageCtx) -> Response:
    job_type = get_job_type(page.services, type_name)
    context: Context = {"job_type": type_view(job_type), "quote": None, "error": None}
    if not job_type.available:
        return render(page, "_quote_panel.html", context)
    form = await page.request.form()
    try:
        context.update(await _price_form(page, job_type, form))
    except HTTPException as error:
        if error.status_code != status.HTTP_422_UNPROCESSABLE_CONTENT:
            raise
        context["error"] = form_error(job_type, error.detail)
    return render(page, "_quote_panel.html", context)


def form_error(job_type: JobType, detail: JsonValue) -> str:
    first = detail[0] if isinstance(detail, list) and detail else None
    if not isinstance(first, dict):
        return FORM_INVALID
    labels = {spec.name: spec.label.lower() for spec in job_type.form.fields}
    location = first.get("loc")
    field = (
        labels.get(str(location[-1])) if isinstance(location, list | tuple) and location else None
    )
    if first.get("type") == "missing":
        return f"fill in {field}" if field else FORM_INVALID
    message = str(first.get("msg", "")).removeprefix("Value error, ").lower()
    return f"check {field}: {message}" if field else f"check the form: {message}"


@router.post("/submit/{type_name}")
async def submit_job(type_name: str, page: PageCtx) -> Response:
    if page.user is None:
        return login_redirect(f"/submit/{type_name}")
    job_type = get_job_type(page.services, type_name)
    form = await verified_form(page.request)
    return await _create_and_redirect(page, page.user, job_type, form)


async def _create_and_redirect(
    page: Page, user: User, job_type: JobType, form: FormData
) -> Response:
    request = QuoteRequest(type=job_type.name, params=params_from_form(form, job_type))
    try:
        _, params, priced = await price_request(page.services, request)
    except HTTPException as error:
        if error.status_code != status.HTTP_422_UNPROCESSABLE_CONTENT:
            raise
        error_text = form_error(job_type, error.detail)
        return _resubmit_page(page, job_type, error_text, request.params, 422)
    except ProbeError:
        return _resubmit_page(page, job_type, PROBE_FAILED, request.params, 502)
    job = create_job(page.session, job_type, params, priced, user)
    try:
        enqueue(page.session, job, user)
    except InsufficientCreditsError:
        page.session.rollback()
        return _resubmit_page(page, job_type, TOPUP_HINT, request.params, 402)
    page.session.commit()
    announce(page.services.bus, job)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


def _job_context(page: Page, job_id: int) -> Context:
    detail = job_detail_view(page.session, page.services, job_id)
    job_type = page.services.catalog.find(detail.type)
    return {
        "detail": detail,
        "job_type": type_view(job_type),
        "panel": running_panel(detail, job_type, detail.events),
        "logs": log_lines(detail.events),
    }


@router.get("/jobs/{job_id}")
async def job_page(job_id: int, page: PageCtx) -> Response:
    return render(page, "job.html", _job_context(page, job_id))


@router.get("/partials/jobs/{job_id}")
async def partial_job(job_id: int, page: PageCtx) -> Response:
    return render(page, "_job_panel.html", _job_context(page, job_id))


def _reserved_credits(session: Session, user: User) -> int:
    query = select(Job.reserved_free, Job.reserved_paid).where(
        Job.owner_id == user.id, Job.status.in_(RESERVED_STATUSES)
    )
    return sum(free + paid for free, paid in session.execute(query))


def _render_wallet(
    page: Page, user: User, error: str | None = None, status_code: int = 200
) -> Response:
    context: Context = {
        "identities": linked_identities(page.session, user),
        "ledger": ledger_entries(page.session, user),
        "reserved": _reserved_credits(page.session, user),
        "credits_per_bean": CREDITS_PER_BEAN,
        "free_daily": FREE_DAILY_CREDITS,
        "csrf_token": csrf_token(page.request),
        "active": "wallet",
        "error": error,
    }
    return render(page, "wallet.html", context, status_code)


@router.get("/wallet")
async def wallet_page(page: PageCtx) -> Response:
    if page.user is None:
        return login_redirect("/wallet")
    return _render_wallet(page, page.user)


@router.post("/wallet/topup")
async def wallet_topup(page: PageCtx) -> Response:
    if page.user is None:
        return login_redirect("/wallet")
    form = await verified_form(page.request)
    try:
        body = TopupRequest.model_validate({"beans": form_str(form, "beans")})
    except ValidationError:
        error = f"choose between 1 and {MAX_TOPUP_BEANS} beans"
        return _render_wallet(page, page.user, error, 422)
    url = topup_url(page.services.settings.beans_url, page.user.username, body.beans)
    return RedirectResponse(url, status_code=303)


@router.post("/wallet/unlink")
async def wallet_unlink(page: PageCtx) -> Response:
    if page.user is None:
        return login_redirect("/wallet")
    form = await verified_form(page.request)
    unlink_identity(page.session, page.user, form_str(form, "identity"))
    return RedirectResponse("/wallet", status_code=303)


async def _admin_redirect(page: Page) -> RedirectResponse | None:
    if page.user is None:
        return login_redirect("/admin")
    await require_admin(page.user, page.services)
    return None


def _render_admin(page: Page, error: str | None = None, status_code: int = 200) -> Response:
    queue = queue_view(page.session, page.services)
    reserved = {queue.running.id} if queue.running else set()
    reserved |= {slot.id for slot in queue.queued}
    recent_jobs = [
        job
        for job in job_views(page.session, page.services, None, ADMIN_JOB_FETCH)
        if job.id not in reserved
    ][:ADMIN_RECENT_JOBS]
    context: Context = {
        "queue": queue,
        "recent_jobs": recent_jobs,
        "health": health(page.services),
        "csrf_token": csrf_token(page.request),
        "active": "admin",
        "error": error,
    }
    return render(page, "admin.html", context, status_code)


@router.get("/admin")
async def admin_page(page: PageCtx) -> Response:
    return await _admin_redirect(page) or _render_admin(page)


@router.post("/admin/pause")
async def admin_pause(page: PageCtx) -> Response:
    return await _toggle_pause(page, True)


@router.post("/admin/resume")
async def admin_resume(page: PageCtx) -> Response:
    return await _toggle_pause(page, False)


async def _toggle_pause(page: Page, paused: bool) -> Response:
    redirect = await _admin_redirect(page)
    if redirect:
        return redirect
    await verified_form(page.request)
    set_paused(page.services, paused)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/cancel")
async def admin_cancel(page: PageCtx) -> Response:
    redirect = await _admin_redirect(page)
    if redirect:
        return redirect
    form = await verified_form(page.request)
    job_id = form_str(form, "job_id")
    if not job_id.isdigit():
        return _render_admin(page, "enter a job id", 422)
    try:
        cancel_job(page.session, page.services, int(job_id))
    except HTTPException as error:
        return _render_admin(page, str(error.detail), error.status_code)
    except JobError as error:
        return _render_admin(page, str(error), status.HTTP_409_CONFLICT)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/jobs/{job_id}/remove")
async def admin_remove(job_id: int, page: PageCtx) -> Response:
    redirect = await _admin_redirect(page)
    if redirect:
        return redirect
    await verified_form(page.request)
    try:
        await remove_job_files(page.session, page.services, job_id)
    except HTTPException as error:
        return _render_admin(page, str(error.detail), error.status_code)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/credits")
async def admin_credits(page: PageCtx) -> Response:
    redirect = await _admin_redirect(page)
    if redirect:
        return redirect
    form = await verified_form(page.request)
    try:
        body = AdjustCreditsRequest.model_validate(
            {"credits": form_str(form, "credits"), "note": form_str(form, "note")}
        )
        adjust_credits(page.session, form_str(form, "username"), body)
    except ValidationError:
        return _render_admin(page, "enter a whole number of credits and a note", 422)
    except HTTPException as error:
        return _render_admin(page, str(error.detail), error.status_code)
    except InsufficientCreditsError as error:
        return _render_admin(page, str(error), status.HTTP_402_PAYMENT_REQUIRED)
    return RedirectResponse("/admin", status_code=303)


@router.get("/runs")
async def runs_page(
    page: PageCtx,
    type_name: Annotated[str | None, Query(alias="type")] = None,
    before: int | None = None,
) -> Response:
    jobs = finished_job_views(page.session, page.services, type_name, before, RUNS_PAGE + 1)
    context: Context = {
        "active": "runs",
        "jobs": jobs[:RUNS_PAGE],
        "older": jobs[RUNS_PAGE - 1].id if len(jobs) > RUNS_PAGE else None,
        "selected": type_name,
        "type_names": [job_type.name for job_type in page.services.catalog.all()],
    }
    return render(page, "runs.html", context)


@router.get("/u/{username}")
async def user_page(username: str, page: PageCtx) -> Response:
    user_or_404(page.session, username)
    context: Context = {
        "username": username,
        "profile_owner": username,
        "jobs": job_views(page.session, page.services, username, USER_JOB_LIMIT),
    }
    return render(page, "user.html", context)
