from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import FakeProber, log_in, make_job, make_user, queue_job
from workflows.db import ExternalIdentity
from workflows.jobs import LogEvent, StepEvent, apply_event, start, succeed
from workflows.state import Services


def csrf_from(html: str) -> str:
    return html.split('name="csrf_token" value="')[1].split('"')[0]


def test_home_page_lists_queue_and_types_logged_out(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "queue is empty" in response.text
    assert "~/parody" in response.text
    assert "coming soon" in response.text
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

    assert "now playing" in response.text
    assert "alice" in response.text
    assert "bob" in response.text


def test_partial_queue_returns_fragment_only(client: TestClient) -> None:
    response = client.get("/partials/queue")

    assert response.status_code == 200
    assert "<!doctype html>" not in response.text.lower()


def test_order_page_renders_fields_for_available_type(client: TestClient) -> None:
    response = client.get("/order/song")

    assert response.status_code == 200
    assert 'name="prompt"' in response.text
    assert 'name="seconds"' in response.text


def test_order_page_shows_coming_soon_for_unavailable_type(client: TestClient) -> None:
    response = client.get("/order/voice")

    assert response.status_code == 200
    assert "coming soon" in response.text


def test_order_page_unknown_type_is_404(client: TestClient) -> None:
    assert client.get("/order/nope").status_code == 404


def test_order_quote_preview_shows_price(client: TestClient) -> None:
    response = client.post(
        "/order/song/quote", data={"prompt": "cats", "seconds": "150", "model": "ace-step"}
    )

    assert response.status_code == 200
    assert "credits" in response.text


def test_order_quote_preview_invalid_params_shows_hint(client: TestClient) -> None:
    response = client.post("/order/song/quote", data={"seconds": "150"})

    assert "fill in the required fields" in response.text


def test_order_submit_redirects_to_login_when_logged_out(client: TestClient) -> None:
    response = client.post("/order/song", data={"prompt": "cats"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/order/song"


def test_order_submit_requires_csrf_token(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice"))

    response = client.post("/order/song", data={"csrf_token": "wrong", "prompt": "cats"})

    assert response.status_code == 403


def test_order_submit_creates_job_and_redirects(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice", paid_credits=1000))
    page = client.get("/order/song")

    response = client.post(
        "/order/song",
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


def test_order_submit_invalid_params_rerenders_form(client: TestClient, session: Session) -> None:
    log_in(client, make_user(session, "alice", paid_credits=1000))
    page = client.get("/order/song")

    response = client.post(
        "/order/song", data={"csrf_token": csrf_from(page.text), "seconds": "150"}
    )

    assert response.status_code == 422
    assert "check the form" in response.text


def test_order_submit_insufficient_credits_shows_topup_hint(
    client: TestClient, session: Session, prober: FakeProber
) -> None:
    long_url = "https://youtube.example/watch?v=long"
    prober.durations[long_url] = 400.0
    log_in(client, make_user(session, "alice", paid_credits=0))
    page = client.get("/order/parody")

    response = client.post(
        "/order/parody", data={"csrf_token": csrf_from(page.text), "url": long_url}
    )

    assert response.status_code == 402
    assert "not enough credits" in response.text


def test_job_page_shows_status(client: TestClient, session: Session, services: Services) -> None:
    job = make_job(session, services, None)

    response = client.get(f"/jobs/{job.id}")

    assert response.status_code == 200
    assert "awaiting_confirmation" in response.text


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
    apply_event(session, job, services.registry["song"], LogEvent(kind="log", message="warming up"))
    apply_event(
        session,
        job,
        services.registry["song"],
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
