import os
from collections.abc import Mapping
from dataclasses import dataclass

CREDITS_PER_BEAN = 100
FREE_DAILY_CREDITS = 500
DEFAULT_DATABASE_URL = "sqlite:///data/workflows.db"
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_EXECUTOR_TIMEOUT_FACTOR = 3.0
DEFAULT_FIRST_EVENT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class Settings:
    """Every setting the service reads, all listed in `.env.example`."""

    session_secret: str
    database_url: str = DEFAULT_DATABASE_URL
    base_url: str = DEFAULT_BASE_URL
    service_token: str = ""
    admin_users: frozenset[str] = frozenset()
    admin_token: str = ""
    n8n_url: str = ""
    n8n_api_key: str = ""
    workflow_services: tuple[str, ...] = ()
    workflow_service_token: str = ""
    executor_token: str = ""
    executor_timeout_factor: float = DEFAULT_EXECUTOR_TIMEOUT_FACTOR
    first_event_timeout_seconds: int = DEFAULT_FIRST_EVENT_TIMEOUT_SECONDS
    ytdl_url: str = ""
    ytdl_api_key: str = ""
    logto_endpoint: str = ""
    logto_app_id: str = ""
    logto_app_secret: str = ""
    beans_url: str = ""
    beans_token: str = ""
    beans_payout_user: str = ""
    dev_login: bool = False
    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_use_ssl: bool = True
    inputs_bucket: str = ""
    upload_url: str = ""

    def __post_init__(self) -> None:
        if self.n8n_url and not self.executor_token:
            raise ValueError("WORKFLOWS_EXECUTOR_TOKEN is required when N8N_URL is set")
        if self.workflow_services and not self.workflow_service_token:
            raise ValueError("WORKFLOW_SERVICE_TOKEN is required when WORKFLOW_SERVICES is set")
        if self.beans_token and not self.beans_url:
            raise ValueError("BEANS_URL is required when BEANS_TOKEN is set")
        if self.dev_login and self.base_url.startswith("https://"):
            raise ValueError("DEV_LOGIN cannot be enabled when BASE_URL is https")


def _username_list(raw: str) -> frozenset[str]:
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def load_settings(environ: Mapping[str, str] = os.environ) -> Settings:
    """Read the settings from environment variables."""
    return Settings(
        session_secret=environ["SESSION_SECRET"],
        database_url=environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        base_url=environ.get("BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        service_token=environ.get("SERVICE_TOKEN", ""),
        admin_users=_username_list(environ.get("WORKFLOWS_ADMIN_USERS", "")),
        admin_token=environ.get("ADMIN_TOKEN", ""),
        n8n_url=environ.get("N8N_URL", "").rstrip("/"),
        n8n_api_key=environ.get("N8N_API_KEY", ""),
        workflow_services=tuple(
            url.strip().rstrip("/")
            for url in environ.get("WORKFLOW_SERVICES", "").split(",")
            if url.strip()
        ),
        workflow_service_token=environ.get("WORKFLOW_SERVICE_TOKEN", ""),
        executor_token=environ.get("WORKFLOWS_EXECUTOR_TOKEN", ""),
        executor_timeout_factor=float(
            environ.get("EXECUTOR_TIMEOUT_FACTOR", DEFAULT_EXECUTOR_TIMEOUT_FACTOR)
        ),
        first_event_timeout_seconds=int(
            environ.get("FIRST_EVENT_TIMEOUT", DEFAULT_FIRST_EVENT_TIMEOUT_SECONDS)
        ),
        ytdl_url=environ.get("YTDL_URL", "").rstrip("/"),
        ytdl_api_key=environ.get("YTDL_API_KEY", ""),
        logto_endpoint=environ.get("LOGTO_ENDPOINT", ""),
        logto_app_id=environ.get("LOGTO_APP_ID", ""),
        logto_app_secret=environ.get("LOGTO_APP_SECRET", ""),
        beans_url=environ.get("BEANS_URL", "").rstrip("/"),
        beans_token=environ.get("BEANS_TOKEN", ""),
        beans_payout_user=environ.get("BEANS_PAYOUT_USER", ""),
        dev_login=environ.get("DEV_LOGIN", "").strip().lower() == "true",
        minio_endpoint=environ.get("MINIO_ENDPOINT", ""),
        minio_access_key=environ.get("MINIO_ACCESS_KEY", ""),
        minio_secret_key=environ.get("MINIO_SECRET_KEY", ""),
        minio_use_ssl=environ.get("MINIO_USE_SSL", "true").strip().lower() == "true",
        inputs_bucket=environ.get("INPUTS_BUCKET", ""),
        upload_url=environ.get("UPLOAD_URL", ""),
    )
