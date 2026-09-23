from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.responses import Response

from workflows.account import ledger_entries, linked_identities, topup_url
from workflows.api import (
    QuoteRequest,
    get_job_or_404,
    get_job_type,
    job_detail_view,
    job_views,
    price_request,
    queue_view,
    quote_view,
    type_view,
)
from workflows.auth import CurrentUser, csrf_token, is_admin, verify_csrf
from workflows.clients import templates
from workflows.db import Job, JobStatus, JsonObject, User
from workflows.jobs import announce, cancel, create_job, ensure_available, job_type_for
from workflows.jobs import enqueue as enqueue_job
from workflows.jobtypes import JobType
from workflows.ledger import InsufficientCreditsError, adjust
from workflows.settings import CREDITS_PER_BEAN, FREE_DAILY_CREDITS
from workflows.state import AppServices, Db, Services
from workflows.webforms import FieldSpec, field_specs
from workflows.webviews import log_lines, me_chip, running_panel

RESERVED_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)
RECENT_RESULTS = 4
USER_JOB_LIMIT = 50

router = APIRouter()


@dataclass(frozen=True)
class Page:
    request: Request
    session: Session
    services: Services
    user: User | None


async def get_page(request: Request, session: Db, services: AppServices, user: CurrentUser) -> Page:
    return Page(request, session, services, user)


PageCtx = Annotated[Page, Depends(get_page)]


def render(
    page: Page, template: str, context: dict[str, object], status_code: int = 200
) -> Response:
    full: dict[str, object] = dict(context)
    full["me"] = me_chip(page.session, page.services.settings, page.user) if page.user else None
    return templates.TemplateResponse(page.request, template, full, status_code=status_code)


def login_redirect(next_path: str) -> RedirectResponse:
    return RedirectResponse(f"/login?next={next_path}", status_code=303)


def form_int(form: FormData, name: str) -> int:
    raw = form.get(name)
    return int(raw) if isinstance(raw, str) and raw else 0


def form_str(form: FormData, name: str) -> str:
    raw = form.get(name)
    return raw if isinstance(raw, str) else ""


def params_from_form(form: FormData, job_type: JobType) -> JsonObject:
    params: JsonObject = {}
    for spec in field_specs(job_type.params_model.model_json_schema()):
        _apply_field(params, form, spec)
    return params


def _apply_field(params: JsonObject, form: FormData, spec: FieldSpec) -> None:
    if spec.kind == "checkbox":
        params[spec.name] = spec.name in form
        return
    raw = form.get(spec.name)
    if not isinstance(raw, str) or raw == "":
        return
    params[spec.name] = int(raw) if spec.kind == "number" else raw


def _queue_context(session: Session, services: Services) -> dict[str, object]:
    queue = queue_view(session, services)
    running = (
        running_panel(queue.running, job_type_for(services.registry, queue.running.type), [])
        if queue.running
        else None
    )
    types = [type_view(job_type) for job_type in services.registry.values()]
    recent = [
        job for job in job_views(session, services, None, 20) if job.status == JobStatus.SUCCEEDED
    ][:RECENT_RESULTS]
    return {"queue": queue, "running": running, "types": types, "recent": recent}


@router.get("/")
async def home(page: PageCtx) -> Response:
    context = _queue_context(page.session, page.services)
    return render(page, "home.html", {**context, "active": "home"})


@router.get("/partials/queue")
async def partial_queue(page: PageCtx) -> Response:
    return render(page, "_queue_panel.html", _queue_context(page.session, page.services))


@router.get("/order/{type_name}")
async def order_page(type_name: str, page: PageCtx) -> Response:
    job_type = get_job_type(page.services, type_name)
    context = {
        "job_type": type_view(job_type),
        "fields": field_specs(job_type.params_model.model_json_schema()),
        "quote": None,
        "error": None,
        "csrf_token": csrf_token(page.request),
    }
    return render(page, "order.html", context)


@router.post("/order/{type_name}/quote")
async def order_quote(type_name: str, page: PageCtx) -> Response:
    job_type = get_job_type(page.services, type_name)
    form = await page.request.form()
    context: dict[str, object] = {"job_type": type_view(job_type), "quote": None, "error": None}
    if not job_type.available:
        return render(page, "_quote_panel.html", context)
    params_dict = params_from_form(form, job_type)
    try:
        _, _, priced = await price_request(
            page.services, QuoteRequest(type=type_name, params=params_dict)
        )
    except HTTPException as error:
        if error.status_code != status.HTTP_422_UNPROCESSABLE_CONTENT:
            raise
        context["error"] = "fill in the required fields to see your price"
        return render(page, "_quote_panel.html", context)
    context["quote"] = quote_view(priced)
    return render(page, "_quote_panel.html", context)


@router.post("/order/{type_name}")
async def order_submit(type_name: str, page: PageCtx) -> Response:
    if page.user is None:
        return login_redirect(f"/order/{type_name}")
    job_type = get_job_type(page.services, type_name)
    ensure_available(job_type)
    form = await page.request.form()
    csrf = form.get("csrf_token")
    verify_csrf(page.request, csrf if isinstance(csrf, str) else "")
    return await _create_and_redirect(page, page.user, job_type, type_name, form)


async def _create_and_redirect(
    page: Page, user: User, job_type: JobType, type_name: str, form: FormData
) -> Response:
    params_dict = params_from_form(form, job_type)
    context = {
        "job_type": type_view(job_type),
        "fields": field_specs(job_type.params_model.model_json_schema()),
        "quote": None,
        "csrf_token": csrf_token(page.request),
    }
    try:
        _, params, priced = await price_request(
            page.services, QuoteRequest(type=type_name, params=params_dict)
        )
    except HTTPException as error:
        if error.status_code != status.HTTP_422_UNPROCESSABLE_CONTENT:
            raise
        return render(page, "order.html", {**context, "error": "check the form for mistakes"}, 422)
    job = create_job(page.session, job_type, params, priced, user)
    try:
        enqueue_job(page.session, job, user)
    except InsufficientCreditsError:
        page.session.rollback()
        hint = "not enough credits, top up in your wallet first"
        return render(page, "order.html", {**context, "error": hint}, 402)
    page.session.commit()
    announce(page.services.bus, job)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.get("/jobs/{job_id}")
async def job_page(job_id: int, page: PageCtx) -> Response:
    detail = job_detail_view(page.session, page.services, job_id)
    job_type = job_type_for(page.services.registry, detail.type)
    context = {
        "detail": detail,
        "job_type": type_view(job_type),
        "panel": running_panel(detail, job_type, detail.events),
        "logs": log_lines(detail.events),
    }
    return render(page, "job.html", context)


@router.get("/partials/jobs/{job_id}")
async def partial_job(job_id: int, page: PageCtx) -> Response:
    detail = job_detail_view(page.session, page.services, job_id)
    job_type = job_type_for(page.services.registry, detail.type)
    context = {
        "detail": detail,
        "job_type": type_view(job_type),
        "panel": running_panel(detail, job_type, detail.events),
        "logs": log_lines(detail.events),
    }
    return render(page, "_job_panel.html", context)


def _reserved_credits(session: Session, user: User) -> int:
    query = select(Job.reserved_free, Job.reserved_paid).where(
        Job.owner_id == user.id, Job.status.in_(RESERVED_STATUSES)
    )
    return sum(free + paid for free, paid in session.execute(query))


@router.get("/wallet")
async def wallet_page(page: PageCtx) -> Response:
    if page.user is None:
        return login_redirect("/wallet")
    context = {
        "identities": linked_identities(page.session, page.user),
        "ledger": ledger_entries(page.session, page.user),
        "reserved": _reserved_credits(page.session, page.user),
        "credits_per_bean": CREDITS_PER_BEAN,
        "free_daily": FREE_DAILY_CREDITS,
        "csrf_token": csrf_token(page.request),
        "active": "wallet",
    }
    return render(page, "wallet.html", context)


@router.post("/wallet/topup")
async def wallet_topup(page: PageCtx) -> Response:
    if page.user is None:
        return login_redirect("/wallet")
    form = await page.request.form()
    csrf = form.get("csrf_token")
    verify_csrf(page.request, csrf if isinstance(csrf, str) else "")
    beans = form_int(form, "beans")
    url = topup_url(page.services.settings.beans_url, page.user.username, beans)
    return RedirectResponse(url, status_code=303)


def _require_admin_page(page: Page) -> RedirectResponse | None:
    if page.user is None:
        return login_redirect("/admin")
    require_admin_check(page.user, page.services)
    return None


def require_admin_check(user: User, services: Services) -> None:
    if not is_admin(user, services.settings):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admins only")


def _health(services: Services) -> dict[str, object]:
    return {
        "executors": {name: job_type.available for name, job_type in services.registry.items()},
        "beans_poller_last_success": services.beans_poller.last_success
        if services.beans_poller
        else None,
    }


@router.get("/admin")
async def admin_page(page: PageCtx) -> Response:
    redirect = _require_admin_page(page)
    if redirect:
        return redirect
    context = {
        "queue": queue_view(page.session, page.services),
        "health": _health(page.services),
        "csrf_token": csrf_token(page.request),
        "active": "admin",
    }
    return render(page, "admin.html", context)


@router.post("/admin/pause")
async def admin_pause(page: PageCtx) -> Response:
    return await _toggle_pause(page, True)


@router.post("/admin/resume")
async def admin_resume(page: PageCtx) -> Response:
    return await _toggle_pause(page, False)


async def _toggle_pause(page: Page, paused: bool) -> Response:
    redirect = _require_admin_page(page)
    if redirect:
        return redirect
    form = await page.request.form()
    csrf = form.get("csrf_token")
    verify_csrf(page.request, csrf if isinstance(csrf, str) else "")
    page.services.worker.paused = paused
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/cancel")
async def admin_cancel(page: PageCtx) -> Response:
    redirect = _require_admin_page(page)
    if redirect:
        return redirect
    form = await page.request.form()
    csrf = form.get("csrf_token")
    verify_csrf(page.request, csrf if isinstance(csrf, str) else "")
    job = get_job_or_404(page.session, form_int(form, "job_id"))
    cancel(page.session, job)
    page.session.commit()
    announce(page.services.bus, job)
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/credits")
async def admin_credits(page: PageCtx) -> Response:
    redirect = _require_admin_page(page)
    if redirect:
        return redirect
    form = await page.request.form()
    csrf = form.get("csrf_token")
    verify_csrf(page.request, csrf if isinstance(csrf, str) else "")
    username = form_str(form, "username")
    target = page.session.scalar(select(User).where(User.username == username))
    if target is not None:
        adjust(page.session, target, form_int(form, "credits"), form_str(form, "note"))
        page.session.commit()
    return RedirectResponse("/admin", status_code=303)


@router.get("/u/{username}")
async def user_page(username: str, page: PageCtx) -> Response:
    target = page.session.scalar(select(User).where(User.username == username))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no user named {username}")
    context: dict[str, object] = {
        "username": username,
        "jobs": job_views(page.session, page.services, username, USER_JOB_LIMIT),
    }
    return render(page, "user.html", context)
