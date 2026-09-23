from fastmcp import FastMCP
from pydantic import BaseModel, Field

from workflows.api import (
    DEFAULT_JOB_LIMIT,
    MAX_JOB_LIMIT,
    JobDetailView,
    JobTypeView,
    JobView,
    QueueView,
    QuoteRequest,
    QuoteView,
    job_detail_view,
    job_views,
    price_request,
    queue_view,
    quote_view,
    type_view,
)
from workflows.db import JsonObject
from workflows.state import Services

MCP_PATH = "/"


class HowToOrderEntry(BaseModel):
    type: str = Field(description="Job type name.")
    available: bool = Field(description="Whether the job type accepts orders now.")
    web_url: str = Field(description="Web page to order this job type.")
    irc_command: str = Field(description="IRC command form to order this job type.")


def build_mcp(services: Services) -> FastMCP:
    mcp: FastMCP = FastMCP("h4ks workflows")

    @mcp.tool
    def list_job_types() -> list[JobTypeView]:
        """List every job type: pricing, steps and parameter schema."""
        return [type_view(job_type) for job_type in services.registry.values()]

    @mcp.tool
    async def quote(type: str, params: JsonObject) -> QuoteView:
        """Price a job without ordering it."""
        _, _, priced = await price_request(services, QuoteRequest(type=type, params=params))
        return quote_view(priced)

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
    def how_to_order() -> list[HowToOrderEntry]:
        """Show how to order each job type on the web and on IRC."""
        base_url = services.settings.base_url
        return [
            HowToOrderEntry(
                type=job_type.name,
                available=job_type.available,
                web_url=f"{base_url}/order/{job_type.name}",
                irc_command=f".wf {job_type.name} ...",
            )
            for job_type in services.registry.values()
        ]

    return mcp
