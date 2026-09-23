import asyncio
import logging
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from workflows.db import JsonObject, User, utcnow
from workflows.ledger import topup
from workflows.settings import Settings

logger = logging.getLogger(__name__)

POLL_SECONDS = 30.0
FETCH_TIMEOUT_SECONDS = 15.0
TRANSACTIONS_PATH = "/api/v1/transactions"
WORKFLOWS_WALLET = "workflows"


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
            await self.tick()
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

    async def _fetch(self) -> list[JsonObject]:
        try:
            response = await self._http.get(
                f"{self._settings.beans_url}{TRANSACTIONS_PATH}",
                headers={"Authorization": f"Bearer {self._settings.beans_token}"},
                timeout=FETCH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise BeansPollError(str(error)) from error
        if not isinstance(body, list):
            raise BeansPollError("beans returned an unexpected shape")
        return body

    def _credit(self, session: Session, transaction: JsonObject) -> None:
        if transaction.get("to_user") != WORKFLOWS_WALLET:
            return
        from_user = transaction.get("from_user")
        amount = transaction.get("amount")
        txn_id = transaction.get("id")
        if not isinstance(from_user, str) or not isinstance(amount, int) or txn_id is None:
            return
        user = session.scalar(select(User).where(User.username == from_user))
        if user is not None:
            topup(session, user, amount, str(txn_id))
