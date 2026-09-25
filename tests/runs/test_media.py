import json
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import PARODY_EXECUTOR
from conftest import SONG_URL
from conftest import assert_ledger_matches
from conftest import make_user
from workflows.db import Job
from workflows.db import JobEvent
from workflows.db import JobStatus
from workflows.jobtypes.catalog import Quote
from workflows.runs.jobs import create_job
from workflows.runs.jobs import enqueue
from workflows.runs.media import MediaStager
from workflows.runs.media import StagingError
from workflows.runs.media import media_fields
from workflows.runs.worker import QueueWorker
from workflows.state import Services

YTDL = "https://ytdl.test"


class KeptFiles:
    def __init__(self) -> None:
        self.kept: dict[str, bytes] = {}

    async def keep(self, key: str, path: Path, content_type: str) -> str:
        self.kept[key] = path.read_bytes()
        return f"https://private.test/{key}?signature=x"


def stager(store: KeptFiles) -> MediaStager:
    return MediaStager(httpx.AsyncClient(), YTDL, "ytdl-key", store)


@respx.mock
async def test_stage_keeps_the_downloaded_file_private() -> None:
    route = respx.post(f"{YTDL}/v1/file").respond(
        200,
        content=b"ID3audio",
        headers={
            "content-type": "audio/mpeg",
            "x-media-title": "Caf%C3%A9%20Song",
            "x-media-duration": "213.5",
        },
    )
    store = KeptFiles()

    staged = await stager(store).stage("7/url", SONG_URL)

    assert staged.url == "https://private.test/7/url.mp3?signature=x"
    assert (staged.title, staged.duration_seconds) == ("Café Song", 213.5)
    assert store.kept == {"7/url.mp3": b"ID3audio"}
    request = route.calls.last.request
    assert request.headers["x-api-key"] == "ytdl-key"
    assert json.loads(request.content) == {"url": SONG_URL, "mode": "audio"}


@respx.mock
async def test_stage_reports_why_the_downloader_refused() -> None:
    respx.post(f"{YTDL}/v1/file").respond(422, json={"detail": "live streams have no end"})

    with pytest.raises(StagingError, match="live streams have no end"):
        await stager(KeptFiles()).stage("7/url", SONG_URL)


def test_only_audio_link_fields_are_downloaded(services: Services) -> None:
    assert media_fields(services.catalog.find("parody").form) == ["url"]
    assert media_fields(services.catalog.find("song").form) == []


def queue_parody(session: Session, services: Services) -> int:
    user = make_user(session, "alice")
    parody = services.catalog.find("parody")
    params = parody.validate({"prompt": "about cats", "url": SONG_URL})
    job = create_job(session, parody, params, Quote(100, 60, None), user)
    enqueue(session, job, user)
    session.commit()
    return job.id


def worker_with(services: Services, store: KeptFiles) -> QueueWorker:
    return QueueWorker(
        services.sessions, services.catalog, services.bus, services.settings, stager(store)
    )


@respx.mock
async def test_the_executor_gets_the_private_file_and_what_it_is(
    session: Session, services: Services
) -> None:
    respx.post(f"{YTDL}/v1/file").respond(
        200,
        content=b"ID3",
        headers={"content-type": "audio/mpeg", "x-media-title": "Hit", "x-media-duration": "90"},
    )
    executor = respx.post(PARODY_EXECUTOR).respond(202)
    job_id = queue_parody(session, services)

    await worker_with(services, KeptFiles()).tick()

    payload = json.loads(executor.calls.last.request.content)
    assert payload["params"]["url"] == f"https://private.test/{job_id}/url.mp3?signature=x"
    assert payload["media"] == {"url": {"title": "Hit", "duration": 90.0}}
    logs = session.scalars(select(JobEvent.data).where(JobEvent.job_id == job_id)).all()
    assert [log["message"] for log in logs] == [f"downloading {SONG_URL}", "downloaded Hit (1:30)"]


@respx.mock
async def test_a_link_we_cannot_download_fails_the_job_and_refunds(
    session: Session, services: Services
) -> None:
    respx.post(f"{YTDL}/v1/file").respond(422, json={"detail": "private video"})
    executor = respx.post(PARODY_EXECUTOR).respond(202)
    job_id = queue_parody(session, services)

    await worker_with(services, KeptFiles()).tick()

    session.expire_all()
    failed = session.get_one(Job, job_id)
    assert failed.status == JobStatus.FAILED
    assert failed.error == f"could not download {SONG_URL}: private video"
    assert not executor.called
    assert failed.owner is not None
    assert_ledger_matches(session, failed.owner)
