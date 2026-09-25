from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, cast

import httpx
from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from workflows.accounts.beans import BeansPoller
from workflows.jobtypes.catalog import Catalog
from workflows.jobtypes.probe import Prober
from workflows.runs.bus import EventBus
from workflows.runs.worker import QueueWorker
from workflows.settings import Settings
from workflows.storage import Storage


@dataclass(frozen=True)
class Services:
    settings: Settings
    sessions: sessionmaker[Session]
    catalog: Catalog
    bus: EventBus
    prober: Prober
    worker: QueueWorker
    http: httpx.AsyncClient
    oauth: OAuth | None
    beans_poller: BeansPoller | None
    storage: Storage | None


def get_services(request: Request) -> Services:
    return cast(Services, request.app.state.services)


AppServices = Annotated[Services, Depends(get_services)]


async def get_db(services: AppServices) -> AsyncIterator[Session]:
    with services.sessions() as session:
        yield session


Db = Annotated[Session, Depends(get_db)]
