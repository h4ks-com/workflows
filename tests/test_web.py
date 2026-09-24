import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import FakeProber, log_in, make_job, make_user, queue_job
from test_admin import MINIO_ENDPOINT, FakeStorage, with_storage
from workflows.db import ExternalIdentity, JsonObject
from workflows.jobs import LogEvent, StepEvent, apply_event, start, succeed
from workflows.state import Services


def csrf_from(html: str) -> str:
    return html.split('name="csrf_token" value="')[1].split('"')[0]


def test_html_pages_are_not_cached(client: TestClient) -> None:
    response = client.get("/")

    assert response.headers["cache-control"] == "no-store"


def test_static_files_are_not_marked_no_store(client: TestClient) -> None:
    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert response.headers.get("cache-control") != "no-store"


def test_home_page_lists_queue_and_types_logged_out(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "nothing in line" in response.text
    assert "~/parody" in response.text
    assert "not available yet" in response.text
    assert "log in" in response.text


def test_home_page_shows_running_and_queued_jobs(
    client: TestClient, session: Session, services: Services
) -> None:
    alice = make_user(session, "alice")
    running = queue_job(session, services, alice)
    start(running)
    session.commit()
    bob = make_user(session, "bob")
    queue_job(session, services, bob)

    response = client.get("/")

    assert "running now" in response.text
    assert "GPU" not in response.text
    assert 'stroke-dasharray="276.5"' in response.text
    assert "alice" in response.text
    assert "bob" in response.text


def test_partial_queue_returns_fragment_only(client: TestClient) -> None:
    response = client.get("/partials/queue")

    assert response.status_code == 200
    assert "<!doctype html>" not in response.text.lower()


def test_submit_page_renders_fields_for_available_type(client: TestClient) -> None:
    response = client.get("/submit/song")

    assert response.status_code == 200
    assert 'name="prompt"' in response.text
    assert 'name="seconds"' in response.text


def test_submit_page_shows_coming_soon_for_unavailable_type(client: TestClient) -> None:
    response = client.get("/submit/voice")

    assert response.status_code == 200
    assert "not available yet" in response.text


def test_submit_page_unknown_type_is_404(client: TestClient) -> None:
    assert client.get("/submit/nope").status_code == 404


def test_submit_quote_preview_shows_price(client: TestClient) -> None:
    response = client.post(
        "/submit/song/quote", data={"prompt": "cats", "seconds": "150", "model": "ace-step"}
    )

    assert response.status_code == 200
    assert "credits" in response.text


def test_submit_quote_preview_invalid_params_shows_hint(client: TestClient) -> None:
    response = client.post("/submit/song/quote", data={"seconds": "150"})

    assert "fill in prompt to see the cost" in response.text


def test_submit_quote_preview_names_the_invalid_field(client: TestClient) -> None:
    response = client.post("/submit/song/quote", data={"prompt": "cats", "model": "nope"})

    assert "check model: choose one of ace-step, minimax" in response.text


def test_submit_redirects_to_login_when_logged_out(client: TestClient) -> None:
    response = client.post("/submit/song", data={"prompt": "cats"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/submit/song"


def test_submit_requires_csrf_token(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice"))

    response = client.post("/submit/song", data={"csrf_token": "wrong", "prompt": "cats"})

    assert response.status_code == 403


def test_submit_creates_job_and_redirects(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice", paid_credits=1000))
    page = client.get("/submit/song")

    response = client.post(
        "/submit/song",
        data={
            "csrf_token": csrf_from(page.text),
            "prompt": "cats",
            "seconds": "150",
            "model": "ace-step",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")


def test_submit_invalid_params_rerenders_form(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice", paid_credits=1000))
    page = client.get("/submit/song")

    response = client.post(
        "/submit/song", data={"csrf_token": csrf_from(page.text), "seconds": "150"}
    )

    assert response.status_code == 422
    assert "check the form" in response.text


def test_submit_insufficient_credits_shows_topup_hint(
    client: TestClient, session: Session, prober: FakeProber
) -> None:
    long_url = "https://youtube.example/watch?v=long"
    prober.durations[long_url] = 400.0
    log_in(client, make_user(session, "alice", paid_credits=0))
    page = client.get("/submit/parody")

    response = client.post(
        "/submit/parody", data={"csrf_token": csrf_from(page.text), "url": long_url}
    )

    assert response.status_code == 402
    assert "not enough credits" in response.text


def test_job_page_shows_status(client: TestClient, session: Session, services: Services) -> None:
    job = make_job(session, services, None)

    response = client.get(f"/jobs/{job.id}")

    assert response.status_code == 200
    assert "waiting for confirmation" in response.text


def test_job_page_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/jobs/999999").status_code == 404


def test_job_partial_updates_after_success(
    client: TestClient, session: Session, services: Services
) -> None:
    job = queue_job(session, services, make_user(session, "alice"))
    start(job)
    succeed(
        session,
        job,
        {
            "files": [
                {"url": "https://bucket.example/song.mp3", "name": "song.mp3", "mime": "audio/mpeg"}
            ],
            "title": "My Song",
        },
    )
    session.commit()

    response = client.get(f"/partials/jobs/{job.id}")

    assert response.status_code == 200
    assert "My Song" in response.text
    assert "https://bucket.example/song.mp3" in response.text


def test_job_page_shows_step_progress_and_log(
    client: TestClient, session: Session, services: Services
) -> None:
    job = queue_job(session, services, make_user(session, "alice"))
    start(job)
    session.commit()
    apply_event(
        session, job, services.catalog.find("song"), LogEvent(kind="log", message="warming up")
    )
    apply_event(
        session,
        job,
        services.catalog.find("song"),
        StepEvent(kind="step", step="write", done=1, total=2),
    )
    session.commit()

    response = client.get(f"/jobs/{job.id}")

    assert response.status_code == 200
    assert "warming up" in response.text
    assert "write" in response.text
    assert "1/2" in response.text


def test_wallet_redirects_when_logged_out(client: TestClient) -> None:
    response = client.get("/wallet", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/wallet"


def test_wallet_shows_balance_when_logged_in(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice", paid_credits=250))

    response = client.get("/wallet")

    assert response.status_code == 200
    assert "750" in response.text


def test_wallet_topup_redirects_to_beans(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice"))
    page = client.get("/wallet")

    response = client.post(
        "/wallet/topup",
        data={"csrf_token": csrf_from(page.text), "beans": "10"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/transfer/alice/workflows/10")


def test_admin_redirects_when_logged_out(client: TestClient) -> None:
    response = client.get("/admin", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/admin"


def test_admin_forbidden_for_non_admins(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice"))

    assert client.get("/admin").status_code == 403


def test_admin_page_for_admin(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "root"))

    response = client.get("/admin")

    assert response.status_code == 200
    assert "pause queue" in response.text


def test_admin_pause_and_resume(client: TestClient, session: Session, services: Services) -> None:
    log_in(client, make_user(session, "root"))
    page = client.get("/admin")

    client.post("/admin/pause", data={"csrf_token": csrf_from(page.text)}, follow_redirects=False)
    assert services.worker.paused is True

    page = client.get("/admin")
    client.post("/admin/resume", data={"csrf_token": csrf_from(page.text)}, follow_redirects=False)
    assert services.worker.paused is False


def test_admin_cancel_refunds_job(client: TestClient, session: Session, services: Services) -> None:
    log_in(client, make_user(session, "root"))
    job = queue_job(session, services, make_user(session, "alice"))
    page = client.get("/admin")

    response = client.post(
        "/admin/cancel",
        data={"csrf_token": csrf_from(page.text), "job_id": str(job.id)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    session.expire_all()
    assert job.status == "cancelled"


def test_admin_grant_credits(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "root"))
    alice = make_user(session, "alice")
    page = client.get("/admin")

    client.post(
        "/admin/credits",
        data={
            "csrf_token": csrf_from(page.text),
            "username": "alice",
            "credits": "300",
            "note": "gift",
        },
        follow_redirects=False,
    )

    session.expire_all()
    assert alice.paid_credits == 300


def test_user_page_shows_job_history(
    client: TestClient, session: Session, services: Services
) -> None:
    alice = make_user(session, "alice")
    make_job(session, services, alice)

    response = client.get("/u/alice")

    assert response.status_code == 200
    assert "song" in response.text


def test_user_page_unknown_user_is_404(client: TestClient) -> None:
    assert client.get("/u/nobody").status_code == 404


def test_wallet_unlinks_an_identity(client: TestClient, session: Session) -> None:
    user = make_user(session, "alice")
    session.add(ExternalIdentity(identity="irc:alice", user_id=user.id))
    session.commit()
    log_in(client, user)
    page = client.get("/wallet")
    assert "irc:alice" in page.text

    response = client.post(
        "/wallet/unlink",
        data={"csrf_token": csrf_from(page.text), "identity": "irc:alice"},
        follow_redirects=False,
    )

    assert response.headers["location"] == "/wallet"
    assert session.scalar(select(ExternalIdentity)) is None


def test_submit_with_non_numeric_input_rerenders_form(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice"))
    page = client.get("/submit/song")

    response = client.post(
        "/submit/song",
        data={"csrf_token": csrf_from(page.text), "prompt": "cats", "seconds": "abc"},
    )
    quote = client.post("/submit/song/quote", data={"prompt": "cats", "seconds": "abc"})

    assert response.status_code == 422
    assert "check the form" in response.text
    assert "check length (seconds): input should be a valid integer" in quote.text


def test_submit_shows_probe_failures(client: TestClient, session: Session) -> None:
    unknown_url = "https://youtube.example/watch?v=missing"
    log_in(client, make_user(session, "alice"))
    page = client.get("/submit/parody")

    quote = client.post("/submit/parody/quote", data={"url": unknown_url})
    response = client.post(
        "/submit/parody", data={"csrf_token": csrf_from(page.text), "url": unknown_url}
    )

    assert "we could not read it" in quote.text
    assert response.status_code == 502
    assert "we could not read it" in response.text


def test_submit_page_uses_textareas_for_long_text(client: TestClient) -> None:
    text = client.get("/submit/song").text

    assert '<textarea id="f-prompt"' in text
    assert '<input id="f-style" type="text"' in text


@pytest.mark.parametrize("beans", ["0", "1001", "abc"])
def test_wallet_topup_rejects_out_of_range_beans(
    client: TestClient, session: Session, beans: str
) -> None:
    log_in(client, make_user(session, "alice"))
    page = client.get("/wallet")

    response = client.post(
        "/wallet/topup", data={"csrf_token": csrf_from(page.text), "beans": beans}
    )

    assert response.status_code == 422
    assert "choose between 1 and 1000 beans" in response.text


@pytest.mark.parametrize(
    ("form", "status", "message"),
    [
        ({"username": "nobody", "credits": "5", "note": "x"}, 404, "no user named nobody"),
        ({"username": "alice", "credits": "lots", "note": "x"}, 422, "whole number of credits"),
        ({"username": "alice", "credits": "-5", "note": "x"}, 402, "needs 5 credits"),
    ],
)
def test_admin_grant_errors_render_the_page(
    client: TestClient, session: Session, form: dict[str, str], status: int, message: str
) -> None:
    log_in(client, make_user(session, "root"))
    make_user(session, "alice")
    page = client.get("/admin")

    response = client.post("/admin/credits", data={"csrf_token": csrf_from(page.text), **form})

    assert response.status_code == status
    assert message in response.text


def test_admin_cancel_errors_render_the_page(
    client: TestClient, session: Session, services: Services
) -> None:
    log_in(client, make_user(session, "root"))
    job = queue_job(session, services, make_user(session, "alice"))
    job.status = "succeeded"
    session.commit()
    csrf = csrf_from(client.get("/admin").text)

    bad = client.post("/admin/cancel", data={"csrf_token": csrf, "job_id": "abc"})
    missing = client.post("/admin/cancel", data={"csrf_token": csrf, "job_id": "999"})
    finished = client.post("/admin/cancel", data={"csrf_token": csrf, "job_id": str(job.id)})

    assert (bad.status_code, missing.status_code, finished.status_code) == (422, 404, 409)
    assert "job is already succeeded" in finished.text


def test_results_play_audio_and_link_other_files(
    client: TestClient, session: Session, services: Services
) -> None:
    job = queue_job(session, services, make_user(session, "alice"))
    start(job)
    video: JsonObject = {
        "url": "https://bucket.example/clip.mp4",
        "name": "clip.mp4",
        "mime": "video/mp4",
    }
    succeed(session, job, {"files": [video]})
    session.commit()

    panel = client.get(f"/partials/jobs/{job.id}").text
    home = client.get("/").text

    assert 'class="play"' not in panel
    assert 'href="https://bucket.example/clip.mp4"' in panel
    assert 'data-src="https://bucket.example/clip.mp4"' not in home
    assert 'href="https://bucket.example/clip.mp4"' in home


def test_submit_page_prefills_from_an_earlier_job(
    client: TestClient, session: Session, services: Services
) -> None:
    job = make_job(session, services, None)

    response = client.get(f"/submit/song?from={job.id}")

    assert response.status_code == 200
    assert "a song about cats</textarea>" in response.text


def test_submit_page_ignores_a_job_of_another_type(
    client: TestClient, session: Session, services: Services
) -> None:
    job = make_job(session, services, None)

    response = client.get(f"/submit/parody?from={job.id}")

    assert "a song about cats" not in response.text


def test_job_page_offers_to_run_it_again(
    client: TestClient, session: Session, services: Services
) -> None:
    job = make_job(session, services, None)

    assert f"/submit/song?from={job.id}" in client.get(f"/jobs/{job.id}").text


def test_admin_page_lists_jobs_with_actions(
    client: TestClient, session: Session, services: Services
) -> None:
    log_in(client, make_user(session, "root"))
    alice = make_user(session, "alice")
    job = queue_job(session, services, alice)

    response = client.get("/admin")

    assert response.status_code == 200
    assert f"/jobs/{job.id}" in response.text
    assert "cancel and refund" in response.text
    assert 'placeholder="job id"' not in response.text


def test_admin_page_offers_remove_files_for_jobs_with_results(
    client: TestClient, session: Session, services: Services
) -> None:
    log_in(client, make_user(session, "root"))
    job = queue_job(session, services, make_user(session, "alice"))
    start(job)
    succeed(session, job, {"files": [{"url": "x", "name": "x", "mime": "audio/mpeg"}]})
    session.commit()

    response = client.get("/admin")

    assert f'action="/admin/jobs/{job.id}/remove"' in response.text
    assert "remove files" in response.text


def test_admin_removes_job_files_via_web_form(
    client: TestClient, session: Session, services: Services, app: FastAPI
) -> None:
    storage = FakeStorage()
    with_storage(app, services, storage)
    log_in(client, make_user(session, "root"))
    job = queue_job(session, services, make_user(session, "alice"))
    start(job)
    succeed(
        session,
        job,
        {"files": [{"url": f"https://{MINIO_ENDPOINT}/workflows/x.mp3", "name": "x", "mime": "x"}]},
    )
    session.commit()
    page = client.get("/admin")

    response = client.post(
        f"/admin/jobs/{job.id}/remove",
        data={"csrf_token": csrf_from(page.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    session.expire_all()
    assert job.removed_at is not None
    assert storage.removed == [("workflows", "x.mp3")]


def test_admin_remove_job_files_without_storage_renders_error(
    client: TestClient, session: Session, services: Services
) -> None:
    log_in(client, make_user(session, "root"))
    job = queue_job(session, services, make_user(session, "alice"))
    start(job)
    succeed(session, job, {"files": [{"url": "x", "name": "x", "mime": "x"}]})
    session.commit()
    page = client.get("/admin")

    response = client.post(
        f"/admin/jobs/{job.id}/remove", data={"csrf_token": csrf_from(page.text)}
    )

    assert response.status_code == 409
    assert "storage is not configured" in response.text


def test_removed_job_hides_result_and_home_feed(
    client: TestClient, session: Session, services: Services, app: FastAPI
) -> None:
    storage = FakeStorage()
    with_storage(app, services, storage)
    job = queue_job(session, services, make_user(session, "alice"))
    start(job)
    succeed(
        session,
        job,
        {"files": [{"url": f"https://{MINIO_ENDPOINT}/workflows/x.mp3", "name": "x", "mime": "x"}]},
    )
    session.commit()
    log_in(client, make_user(session, "root"))
    page = client.get("/admin")
    client.post(
        f"/admin/jobs/{job.id}/remove",
        data={"csrf_token": csrf_from(page.text)},
        follow_redirects=False,
    )

    job_page = client.get(f"/jobs/{job.id}")
    home = client.get("/")

    assert "removed by an admin" in job_page.text
    assert f"/jobs/{job.id}" not in home.text
