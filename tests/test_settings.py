import pytest

from workflows.settings import DEFAULT_DATABASE_URL, Settings, load_settings


def test_load_settings_reads_environment() -> None:
    settings = load_settings(
        {
            "SESSION_SECRET": "secret",
            "BASE_URL": "https://workflows.h4ks.com/",
            "WORKFLOWS_ADMIN_USERS": "alice, bob,,",
            "EXECUTOR_URL_PARODY": "https://n8n/webhook/parody",
            "WORKFLOWS_EXECUTOR_TOKEN": "token",
            "EXECUTOR_TIMEOUT_FACTOR": "4",
        }
    )

    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.base_url == "https://workflows.h4ks.com"
    assert settings.admin_users == {"alice", "bob"}
    assert settings.executor_urls == {"parody": "https://n8n/webhook/parody"}
    assert settings.executor_timeout_factor == 4.0


def test_session_secret_is_required() -> None:
    with pytest.raises(KeyError):
        load_settings({})


def test_executor_url_requires_executor_token() -> None:
    with pytest.raises(ValueError, match="WORKFLOWS_EXECUTOR_TOKEN"):
        Settings(session_secret="secret", executor_urls={"song": "https://n8n/webhook/song"})
