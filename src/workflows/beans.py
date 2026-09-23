import asyncio
import logging
from datetime import datetime

import httpx
from pydantic import BaseModel, Field, StrictInt, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from workflows.db import User, utcnow
from workflows.ledger import topup
from workflows.settings import Settings

logger = logging.getLogger(__name__)

POLL_SECONDS = 30.0
FETCH_TIMEOUT_SECONDS = 15.0
TRANSACTIONS_PATH = "/api/v1/transactions"
WORKFLOWS_WALLET = "workflows"


class BeansTransaction(BaseModel):
    id: int | str
    from_user: str
    to_user: str
    amount: StrictInt = Field(gt=0)


TRANSACTIONS = TypeAdapter(list[BeansTransaction])


class BeansPollError(Exception):
    pass


class BeansPoller:
    def __init__(
        self, sessions: sessionmaker[Session], http: httpx.AsyncClient, settings: Settings
    ) -> None:
        self._sessions = sessions
        self._http = http
        self._settings = settings
        self.last_success: datetime | None = None

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except SQLAlchemyError:
                logger.exception("beans poll could not credit transactions")
            await asyncio.sleep(POLL_SECONDS)

    async def tick(self) -> None:
        try:
            transactions = await self._fetch()
        except BeansPollError as error:
            logger.warning("beans poll failed: %s", error)
            return
        with self._sessions.begin() as session:
            for transaction in transactions:
                self._credit(session, transaction)
        self.last_success = utcnow()

    async def _fetch(self) -> list[BeansTransaction]:
        try:
            response = await self._http.get(
                f"{self._settings.beans_url}{TRANSACTIONS_PATH}",
                headers={"Authorization": f"Bearer {self._settings.beans_token}"},
                timeout=FETCH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return TRANSACTIONS.validate_json(response.content)
        except (httpx.HTTPError, ValidationError) as error:
            raise BeansPollError(str(error)) from error

    def _credit(self, session: Session, transaction: BeansTransaction) -> None:
        if transaction.to_user != WORKFLOWS_WALLET:
            return
        user = session.scalar(select(User).where(User.username == transaction.from_user))
        if user is not None:
            topup(session, user, transaction.amount, str(transaction.id))
