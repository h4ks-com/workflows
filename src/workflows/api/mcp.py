from fastmcp import FastMCP
from pydantic import BaseModel
from pydantic import Field

from workflows.api.drafts import DraftView
from workflows.api.drafts import share_form
from workflows.api.routes import DEFAULT_JOB_LIMIT
from workflows.api.routes import MAX_JOB_LIMIT
from workflows.api.routes import QuoteRequest
from workflows.api.routes import price_request
from workflows.api.views import JobDetailView
from workflows.api.views import JobTypeView
from workflows.api.views import JobView
from workflows.api.views import QueueView
from workflows.api.views import QuoteView
from workflows.api.views import job_detail_view
from workflows.api.views import job_views
from workflows.api.views import queue_view
from workflows.api.views import quote_view
from workflows.api.views import type_view
from workflows.db import JsonObject
from workflows.state import Services


class HowToSubmitEntry(BaseModel):
    type: str = Field(description="Job type name.")
    available: bool = Field(description="Whether the job type accepts submissions now.")
    web_url: str = Field(description="Web page to submit this job type.")


def build_mcp(services: Services) -> FastMCP:
    mcp: FastMCP = FastMCP("h4ks workflows")

    @mcp.tool
    def list_job_types() -> list[JobTypeView]:
        """List every job type: pricing, steps and parameter schema."""
        return [type_view(job_type) for job_type in services.catalog.all()]

    @mcp.tool
    async def quote(type: str, params: JsonObject) -> QuoteView:
        """Price a job without submitting it."""
        _, _, priced = await price_request(services, QuoteRequest(type=type, params=params))
        return quote_view(priced)

    @mcp.tool
    async def share_filled_form(type: str, params: JsonObject) -> DraftView:
        """Fill a job's form and get a short link to it, so someone can review it and submit.

        No login is needed to make the link; the person who opens it logs in to submit.
        Leave out fields you do not know. The link works for 7 days and for one submission.
        """
        return await share_form(services, QuoteRequest(type=type, params=params))

    @mcp.tool
    def get_queue() -> QueueView:
        """Show the job running now and every job waiting in line."""
        with services.sessions() as session:
            return queue_view(session, services)

    @mcp.tool
    def get_job(job_id: int) -> JobDetailView:
        """Show one job's status, progress, result and event history."""
        with services.sessions() as session:
            return job_detail_view(session, services, job_id)

    @mcp.tool
    def list_jobs(user: str | None = None, limit: int = DEFAULT_JOB_LIMIT) -> list[JobView]:
        """List recent jobs, newest first, optionally filtered to one user."""
        limit = max(1, min(limit, MAX_JOB_LIMIT))
        with services.sessions() as session:
            return job_views(session, services, user, limit)

    @mcp.tool
    def how_to_submit() -> list[HowToSubmitEntry]:
        """Show the web submit URL for each job type; jobs are submitted only there, logged in."""
        base_url = services.settings.base_url
        return [
            HowToSubmitEntry(
                type=job_type.name,
                available=job_type.available,
                web_url=f"{base_url}/submit/{job_type.name}",
            )
            for job_type in services.catalog.all()
        ]

    return mcp
