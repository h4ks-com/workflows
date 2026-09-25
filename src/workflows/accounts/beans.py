import asyncio
import logging
from datetime import datetime

import httpx
from pydantic import BaseModel
from pydantic import Field
from pydantic import StrictInt
from pydantic import TypeAdapter
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from workflows.accounts.ledger import topup
from workflows.db import LedgerEntry
from workflows.db import User
from workflows.db import utcnow
from workflows.settings import Settings

logger = logging.getLogger(__name__)

POLL_SECONDS = 30.0
FETCH_TIMEOUT_SECONDS = 15.0
TRANSACTIONS_PATH = "/api/v1/transactions"
WALLET_PATH = "/api/v1/wallet"
TRANSFER_PATH = "/api/v1/transfer"
WORKFLOWS_WALLET = "workflows"


class BeansTransaction(BaseModel):
    id: int | str
    from_user: str
    to_user: str
    amount: StrictInt = Field(gt=0)


TRANSACTIONS = TypeAdapter(list[BeansTransaction])


def _new_topups(session: Session, transactions: list[BeansTransaction]) -> list[BeansTransaction]:
    topups = [txn for txn in transactions if txn.to_user == WORKFLOWS_WALLET]
    ids = [str(txn.id) for txn in topups]
    known = set(
        session.scalars(select(LedgerEntry.beans_txn_id).where(LedgerEntry.beans_txn_id.in_(ids)))
    )
    return [txn for txn in topups if str(txn.id) not in known]


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
            for transaction in _new_topups(session, transactions):
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
        user = session.scalar(select(User).where(User.username == transaction.from_user))
        if user is not None:
            topup(session, user, transaction.amount, str(transaction.id))


class PayoutError(Exception):
    pass


class BeansWallet(BaseModel):
    bean_amount: StrictInt = Field(ge=0)


class BeansPayout:
    """Sends the whole workflows wallet to the one payout account in the settings."""

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        self._settings = settings
        self._sending = asyncio.Lock()

    @property
    def recipient(self) -> str:
        return self._settings.beans_payout_user

    async def send_all(self) -> int:
        """Send every bean in the workflows wallet to the payout account.

        :return: how many beans were sent.
        :raises PayoutError: when the wallet is empty or Beans refuses.
        """
        async with self._sending:
            balance = await self._balance()
            if balance == 0:
                raise PayoutError("the workflows wallet is empty")
            await self._transfer(balance)
        logger.warning("sent %d beans from the workflows wallet to %s", balance, self.recipient)
        return balance

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.beans_token}"}

    async def _balance(self) -> int:
        try:
            response = await self._http.get(
                f"{self._settings.beans_url}{WALLET_PATH}",
                headers=self._headers(),
                timeout=FETCH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return BeansWallet.model_validate_json(response.content).bean_amount
        except (httpx.HTTPError, ValidationError) as error:
            raise PayoutError(f"could not read the workflows wallet: {error}") from error

    async def _transfer(self, amount: int) -> None:
        # We never let Beans create a wallet, so a misspelt payout account fails the transfer.
        body = {"to_user": self.recipient, "amount": amount, "force": False}
        try:
            response = await self._http.post(
                f"{self._settings.beans_url}{TRANSFER_PATH}",
                json=body,
                headers=self._headers(),
                timeout=FETCH_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as error:
            raise PayoutError(f"could not reach Beans: {error}") from error
        if response.is_error:
            raise PayoutError(f"Beans refused the transfer: {response.text[:200]}")


def build_payout(http: httpx.AsyncClient, settings: Settings) -> BeansPayout | None:
    """Offer the payout only when Beans and a payout account are configured."""
    if not (settings.beans_url and settings.beans_token and settings.beans_payout_user):
        return None
    return BeansPayout(http, settings)
