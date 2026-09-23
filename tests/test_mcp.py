from fastapi import FastAPI
from fastmcp import Client
from sqlalchemy.orm import Session

from conftest import SONG_URL, make_user, queue_job
from workflows.state import Services


async def test_list_job_types_matches_the_rest_api(app: FastAPI, services: Services) -> None:
    async with Client(app.state.mcp) as client:
        result = await client.call_tool("list_job_types", {})

    types = {item.name: item for item in result.data}
    assert types["parody"].available is True
    assert types["voice"].available is False
    assert [(step.name, step.weight) for step in types["song"].steps] == [
        ("write", 20),
        ("generate", 70),
        ("store", 10),
    ]


async def test_quote_probes_media(app: FastAPI) -> None:
    async with Client(app.state.mcp) as client:
        result = await client.call_tool("quote", {"type": "parody", "params": {"url": SONG_URL}})

    assert result.structured_content == {
        "credits": 240,
        "beans": 3,
        "probe": {"duration_seconds": 100.0, "title": "Some Song"},
        "estimate_seconds": 240,
    }


async def test_quote_rejects_an_unavailable_type(app: FastAPI) -> None:
    async with Client(app.state.mcp) as client:
        result = await client.call_tool(
            "quote", {"type": "voice", "params": {}}, raise_on_error=False
        )

    assert result.is_error


async def test_get_queue_and_get_job_reflect_the_running_state(
    app: FastAPI, services: Services, session: Session
) -> None:
    user = make_user(session, "alice")
    job = queue_job(session, services, user)

    async with Client(app.state.mcp) as client:
        queue = await client.call_tool("get_queue", {})
        detail = await client.call_tool("get_job", {"job_id": job.id})

    assert queue.data.queued[0].id == job.id
    assert detail.data.id == job.id
    assert detail.data.events == []


async def test_list_jobs_filters_by_user_and_orders_newest_first(
    app: FastAPI, services: Services, session: Session
) -> None:
    alice = make_user(session, "alice")
    bob = make_user(session, "bob")
    queue_job(session, services, alice)
    second = queue_job(session, services, bob)

    async with Client(app.state.mcp) as client:
        result = await client.call_tool("list_jobs", {"user": "bob"})

    assert [job.id for job in result.data] == [second.id]


async def test_how_to_order_lists_web_and_irc_forms(app: FastAPI) -> None:
    async with Client(app.state.mcp) as client:
        result = await client.call_tool("how_to_order", {})

    entries = {entry.type: entry for entry in result.data}
    assert (
        entries["parody"].available,
        entries["parody"].web_url,
        entries["parody"].irc_command,
    ) == (True, "https://workflows.example/order/parody", ".wf parody ...")
    assert entries["voice"].available is False
