from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import BASE_URL
from conftest import SERVICE_TOKEN
from conftest import csrf_from
from conftest import log_in
from conftest import make_user
from workflows.db import Draft
from workflows.db import Job
from workflows.db import utcnow
from workflows.runs.drafts import create_draft
from workflows.runs.drafts import delete_expired_drafts

SERVICE_HEADERS = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
SONG_DRAFT = {"type": "song", "params": {"prompt": "a song about dragons"}}


def token_of(url: str) -> str:
    return url.rsplit("/d/", 1)[1]


def test_client_shares_a_filled_form(client: TestClient) -> None:
    response = client.post("/api/clients/drafts", json=SONG_DRAFT, headers=SERVICE_HEADERS)

    assert response.status_code == 201
    body = response.json()
    assert body["url"].startswith(f"{BASE_URL}/d/")
    assert body["quote"]["credits"] > 0


def test_a_form_missing_fields_has_no_quote(client: TestClient) -> None:
    response = client.post(
        "/api/clients/drafts", json={"type": "song", "params": {}}, headers=SERVICE_HEADERS
    )

    assert response.status_code == 201
    assert response.json()["quote"] is None


def test_sharing_rejects_unknown_fields_and_types(client: TestClient) -> None:
    unknown_field = {"type": "song", "params": {"colour": "red"}}
    unknown_type = {"type": "nope", "params": {}}

    field = client.post("/api/clients/drafts", json=unknown_field, headers=SERVICE_HEADERS)
    job_type = client.post("/api/clients/drafts", json=unknown_type, headers=SERVICE_HEADERS)

    assert field.status_code == 422
    assert field.json()["detail"] == "song has no colour"
    assert job_type.status_code == 404


def test_sharing_needs_the_service_token(client: TestClient) -> None:
    assert client.post("/api/clients/drafts", json=SONG_DRAFT).status_code == 401


async def test_mcp_shares_a_filled_form(app: FastAPI) -> None:
    async with Client(app.state.mcp) as mcp:
        result = await mcp.call_tool("share_filled_form", SONG_DRAFT)

    assert result.structured_content is not None
    assert str(result.structured_content["url"]).startswith(f"{BASE_URL}/d/")


def test_the_link_opens_the_filled_form(client: TestClient) -> None:
    url = client.post("/api/clients/drafts", json=SONG_DRAFT, headers=SERVICE_HEADERS).json()["url"]
    token = token_of(url)

    opened = client.get(f"/d/{token}", follow_redirects=False)
    page = client.get(opened.headers["location"])

    assert opened.headers["location"] == f"/submit/song?draft={token}"
    assert "a song about dragons" in page.text
    assert f'name="draft" value="{token}"' in page.text
    assert f"next=/submit/song%3Fdraft%3D{token}" in page.text


def test_logging_in_keeps_the_filled_form(client: TestClient) -> None:
    url = client.post("/api/clients/drafts", json=SONG_DRAFT, headers=SERVICE_HEADERS).json()["url"]
    token = token_of(url)

    response = client.post("/submit/song", data={"draft": token}, follow_redirects=False)

    assert response.headers["location"] == f"/login?next=/submit/song?draft={token}"


def test_submitting_uses_up_the_link(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice"))
    url = client.post("/api/clients/drafts", json=SONG_DRAFT, headers=SERVICE_HEADERS).json()["url"]
    token = token_of(url)
    page = client.get(f"/submit/song?draft={token}")
    form = {"csrf_token": csrf_from(page.text), "draft": token, "prompt": "a song about dragons"}

    response = client.post("/submit/song", data=form, follow_redirects=False)

    assert response.status_code == 303
    assert session.scalar(
        select(Job).where(Job.params["prompt"].as_string() == "a song about dragons")
    )
    assert client.get(f"/d/{token}").status_code == 404


def test_an_expired_link_is_gone_and_deleted(client: TestClient, session: Session) -> None:
    draft = create_draft(session, "song", {"prompt": "old"})
    draft.expires_at = utcnow() - timedelta(seconds=1)
    session.commit()

    assert client.get(f"/d/{draft.token}").status_code == 404
    assert client.get(f"/submit/song?draft={draft.token}").status_code == 404
    session.expire_all()
    assert session.scalar(select(Draft)) is None


def test_a_link_for_another_type_is_refused(client: TestClient, session: Session) -> None:
    draft = create_draft(session, "song", {"prompt": "x"})
    session.commit()

    assert client.get(f"/submit/parody?draft={draft.token}").status_code == 404


def test_the_sweep_deletes_only_expired_drafts(session: Session) -> None:
    live = create_draft(session, "song", {"prompt": "live"})
    expired = create_draft(session, "song", {"prompt": "expired"})
    expired.expires_at = utcnow() - timedelta(seconds=1)
    session.commit()

    delete_expired_drafts(session)
    session.commit()

    assert [draft.token for draft in session.scalars(select(Draft))] == [live.token]
