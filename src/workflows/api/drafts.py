from datetime import datetime

from fastapi import HTTPException
from fastapi import status
from pydantic import BaseModel
from pydantic import Field

from workflows.api.routes import QuoteRequest
from workflows.api.routes import get_job_type
from workflows.api.routes import price_request
from workflows.api.views import QuoteView
from workflows.api.views import quote_view
from workflows.jobtypes.probe import ProbeError
from workflows.runs.drafts import create_draft
from workflows.runs.drafts import live_draft_count
from workflows.state import Services

MAX_LIVE_DRAFTS = 5000


class DraftView(BaseModel):
    url: str = Field(description="Short link that opens the form filled in, ready to submit.")
    expires_at: datetime = Field(description="When the link stops working.")
    quote: QuoteView | None = Field(
        description="Price of the filled form, or null while it still misses something."
    )


async def _quote_if_complete(services: Services, request: QuoteRequest) -> QuoteView | None:
    try:
        _, _, priced = await price_request(services, request)
    except ProbeError:
        return None
    except HTTPException as error:
        if error.status_code != status.HTTP_422_UNPROCESSABLE_CONTENT:
            raise
        return None
    return quote_view(priced)


async def share_form(services: Services, request: QuoteRequest) -> DraftView:
    """Store a filled form and return a short link to it; missing fields are left for the user.

    :raises HTTPException: 404 for an unknown job type, 422 for fields the form does not have,
        429 when too many links are live.
    """
    job_type = get_job_type(services, request.type)
    unknown = sorted(set(request.params) - {spec.name for spec in job_type.form.fields})
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{job_type.name} has no {', '.join(unknown)}"
        )
    quote = await _quote_if_complete(services, request)
    with services.sessions.begin() as session:
        if live_draft_count(session) >= MAX_LIVE_DRAFTS:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many form links, try later")
        draft = create_draft(session, job_type.name, request.params)
    return DraftView(
        url=f"{services.settings.base_url}/d/{draft.token}",
        expires_at=draft.expires_at,
        quote=quote,
    )
