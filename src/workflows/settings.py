import os
from collections.abc import Mapping
from dataclasses import dataclass, field

CREDITS_PER_BEAN = 100
FREE_DAILY_CREDITS = 500
DEFAULT_DATABASE_URL = "sqlite:///data/workflows.db"
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_YTDL_URL = "http://ytdl.ytdl.svc.cluster.local:8000"
DEFAULT_BEANS_URL = "https://beans.h4ks.com"
DEFAULT_EXECUTOR_TIMEOUT_FACTOR = 3.0
EXECUTOR_URL_PREFIX = "EXECUTOR_URL_"


@dataclass(frozen=True)
class Settings:
    session_secret: str
    database_url: str = DEFAULT_DATABASE_URL
    base_url: str = DEFAULT_BASE_URL
    service_token: str = ""
    admin_users: frozenset[str] = frozenset()
    executor_urls: Mapping[str, str] = field(default_factory=dict)
    executor_token: str = ""
    executor_timeout_factor: float = DEFAULT_EXECUTOR_TIMEOUT_FACTOR
    ytdl_url: str = DEFAULT_YTDL_URL
    ytdl_api_key: str = ""
    logto_endpoint: str = ""
    logto_app_id: str = ""
    logto_app_secret: str = ""
    beans_url: str = DEFAULT_BEANS_URL
    beans_token: str = ""
    cloudbot_url: str = ""
    cloudbot_token: str = ""

    def __post_init__(self) -> None:
        if any(self.executor_urls.values()) and not self.executor_token:
            raise ValueError("WORKFLOWS_EXECUTOR_TOKEN is required when an executor URL is set")


def _executor_urls(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        name.removeprefix(EXECUTOR_URL_PREFIX).lower(): url
        for name, url in environ.items()
        if name.startswith(EXECUTOR_URL_PREFIX)
    }


def _username_list(raw: str) -> frozenset[str]:
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def load_settings(environ: Mapping[str, str] = os.environ) -> Settings:
    return Settings(
        session_secret=environ["SESSION_SECRET"],
        database_url=environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        base_url=environ.get("BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        service_token=environ.get("SERVICE_TOKEN", ""),
        admin_users=_username_list(environ.get("WORKFLOWS_ADMIN_USERS", "")),
        executor_urls=_executor_urls(environ),
        executor_token=environ.get("WORKFLOWS_EXECUTOR_TOKEN", ""),
        executor_timeout_factor=float(
            environ.get("EXECUTOR_TIMEOUT_FACTOR", DEFAULT_EXECUTOR_TIMEOUT_FACTOR)
        ),
        ytdl_url=environ.get("YTDL_URL", DEFAULT_YTDL_URL).rstrip("/"),
        ytdl_api_key=environ.get("YTDL_API_KEY", ""),
        logto_endpoint=environ.get("LOGTO_ENDPOINT", ""),
        logto_app_id=environ.get("LOGTO_APP_ID", ""),
        logto_app_secret=environ.get("LOGTO_APP_SECRET", ""),
        beans_url=environ.get("BEANS_URL", DEFAULT_BEANS_URL).rstrip("/"),
        beans_token=environ.get("BEANS_TOKEN", ""),
        cloudbot_url=environ.get("CLOUDBOT_URL", "").rstrip("/"),
        cloudbot_token=environ.get("CLOUDBOT_TOKEN", ""),
    )
