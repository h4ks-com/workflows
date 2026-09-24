import pytest

from workflows.settings import DEFAULT_DATABASE_URL, Settings, load_settings


def test_load_settings_reads_environment() -> None:
    settings = load_settings(
        {
            "SESSION_SECRET": "secret",
            "BASE_URL": "https://workflows.h4ks.com/",
            "WORKFLOWS_ADMIN_USERS": "alice, bob,,",
            "N8N_URL": "https://n8n.h4ks.com/",
            "WORKFLOWS_EXECUTOR_TOKEN": "token",
            "EXECUTOR_TIMEOUT_FACTOR": "4",
            "FIRST_EVENT_TIMEOUT": "30",
        }
    )

    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.base_url == "https://workflows.h4ks.com"
    assert settings.admin_users == {"alice", "bob"}
    assert settings.n8n_url == "https://n8n.h4ks.com"
    assert settings.executor_timeout_factor == 4.0
    assert settings.first_event_timeout_seconds == 30


def test_session_secret_is_required() -> None:
    with pytest.raises(KeyError):
        load_settings({})


def test_n8n_url_requires_executor_token() -> None:
    with pytest.raises(ValueError, match="WORKFLOWS_EXECUTOR_TOKEN"):
        Settings(session_secret="secret", n8n_url="https://n8n.h4ks.com")
