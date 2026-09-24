import os
from collections.abc import Mapping
from dataclasses import dataclass

CREDITS_PER_BEAN = 100
FREE_DAILY_CREDITS = 500
DEFAULT_DATABASE_URL = "sqlite:///data/workflows.db"
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_YTDL_URL = "http://ytdl.ytdl.svc.cluster.local:8000"
DEFAULT_BEANS_URL = "https://beans.h4ks.com"
DEFAULT_EXECUTOR_TIMEOUT_FACTOR = 3.0
DEFAULT_FIRST_EVENT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class Settings:
    session_secret: str
    database_url: str = DEFAULT_DATABASE_URL
    base_url: str = DEFAULT_BASE_URL
    service_token: str = ""
    admin_users: frozenset[str] = frozenset()
    n8n_url: str = ""
    executor_token: str = ""
    executor_timeout_factor: float = DEFAULT_EXECUTOR_TIMEOUT_FACTOR
    first_event_timeout_seconds: int = DEFAULT_FIRST_EVENT_TIMEOUT_SECONDS
    ytdl_url: str = DEFAULT_YTDL_URL
    ytdl_api_key: str = ""
    logto_endpoint: str = ""
    logto_app_id: str = ""
    logto_app_secret: str = ""
    beans_url: str = DEFAULT_BEANS_URL
    beans_token: str = ""
    dev_login: bool = False
    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_use_ssl: bool = True

    def __post_init__(self) -> None:
        if self.n8n_url and not self.executor_token:
            raise ValueError("WORKFLOWS_EXECUTOR_TOKEN is required when N8N_URL is set")
        if self.dev_login and self.base_url.startswith("https://"):
            raise ValueError("DEV_LOGIN cannot be enabled when BASE_URL is https")


def _username_list(raw: str) -> frozenset[str]:
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def load_settings(environ: Mapping[str, str] = os.environ) -> Settings:
    return Settings(
        session_secret=environ["SESSION_SECRET"],
        database_url=environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        base_url=environ.get("BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        service_token=environ.get("SERVICE_TOKEN", ""),
        admin_users=_username_list(environ.get("WORKFLOWS_ADMIN_USERS", "")),
        n8n_url=environ.get("N8N_URL", "").rstrip("/"),
        executor_token=environ.get("WORKFLOWS_EXECUTOR_TOKEN", ""),
        executor_timeout_factor=float(
            environ.get("EXECUTOR_TIMEOUT_FACTOR", DEFAULT_EXECUTOR_TIMEOUT_FACTOR)
        ),
        first_event_timeout_seconds=int(
            environ.get("FIRST_EVENT_TIMEOUT", DEFAULT_FIRST_EVENT_TIMEOUT_SECONDS)
        ),
        ytdl_url=environ.get("YTDL_URL", DEFAULT_YTDL_URL).rstrip("/"),
        ytdl_api_key=environ.get("YTDL_API_KEY", ""),
        logto_endpoint=environ.get("LOGTO_ENDPOINT", ""),
        logto_app_id=environ.get("LOGTO_APP_ID", ""),
        logto_app_secret=environ.get("LOGTO_APP_SECRET", ""),
        beans_url=environ.get("BEANS_URL", DEFAULT_BEANS_URL).rstrip("/"),
        beans_token=environ.get("BEANS_TOKEN", ""),
        dev_login=environ.get("DEV_LOGIN", "").strip().lower() == "true",
        minio_endpoint=environ.get("MINIO_ENDPOINT", ""),
        minio_access_key=environ.get("MINIO_ACCESS_KEY", ""),
        minio_secret_key=environ.get("MINIO_SECRET_KEY", ""),
        minio_use_ssl=environ.get("MINIO_USE_SSL", "true").strip().lower() == "true",
    )
