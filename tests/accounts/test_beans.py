import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from conftest import make_user
from workflows.accounts.beans import BeansPayout
from workflows.accounts.beans import BeansPoller
from workflows.accounts.beans import PayoutError
from workflows.accounts.beans import build_payout
from workflows.settings import Settings
from workflows.state import Services

TRANSACTIONS_URL = "https://beans.example/api/v1/transactions"


@pytest.fixture
def poller(services: Services) -> BeansPoller:
    beans_settings = Settings(
        session_secret=services.settings.session_secret,
        database_url=services.settings.database_url,
        base_url=services.settings.base_url,
        beans_url=services.settings.beans_url,
        beans_token="beans-token",
    )
    return BeansPoller(services.sessions, services.http, beans_settings)


@respx.mock
async def test_tick_credits_known_senders_and_skips_others(
    session: Session, services: Services, poller: BeansPoller
) -> None:
    alice = make_user(session, "alice")
    respx.get(TRANSACTIONS_URL).respond(
        json=[
            {"id": 1, "from_user": "alice", "to_user": "workflows", "amount": 3},
            {"id": 2, "from_user": "alice", "to_user": "someone_else", "amount": 9},
            {"id": 3, "from_user": "nobody", "to_user": "workflows", "amount": 4},
        ]
    )

    await poller.tick()

    session.refresh(alice)
    assert alice.paid_credits == 300
    assert poller.last_success is not None


@respx.mock
async def test_tick_is_idempotent_on_the_transaction_id(
    session: Session, poller: BeansPoller
) -> None:
    alice = make_user(session, "alice")
    respx.get(TRANSACTIONS_URL).respond(
        json=[{"id": 1, "from_user": "alice", "to_user": "workflows", "amount": 3}]
    )

    await poller.tick()
    await poller.tick()

    session.refresh(alice)
    assert alice.paid_credits == 300


@respx.mock
@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), httpx.Response(200, json={"not": "a list"})],
)
async def test_tick_swallows_beans_errors(poller: BeansPoller, response: httpx.Response) -> None:
    respx.get(TRANSACTIONS_URL).mock(return_value=response)

    await poller.tick()

    assert poller.last_success is None


@respx.mock
@pytest.mark.parametrize("amount", [True, 0, -3, "3", 2.5])
async def test_tick_rejects_malformed_amounts(
    session: Session, poller: BeansPoller, amount: bool | int | str | float
) -> None:
    alice = make_user(session, "alice")
    respx.get(TRANSACTIONS_URL).respond(
        json=[{"id": 1, "from_user": "alice", "to_user": "workflows", "amount": amount}]
    )

    await poller.tick()

    session.refresh(alice)
    assert alice.paid_credits == 0
    assert poller.last_success is None


async def test_run_survives_database_errors(
    poller: BeansPoller, monkeypatch: pytest.MonkeyPatch
) -> None:
    tick = AsyncMock(
        side_effect=[OperationalError("tick", {}, Exception()), asyncio.CancelledError]
    )
    monkeypatch.setattr(poller, "tick", tick)
    monkeypatch.setattr("workflows.accounts.beans.asyncio.sleep", AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    assert tick.await_count == 2


WALLET_URL = "https://beans.example/api/v1/wallet"
TRANSFER_URL = "https://beans.example/api/v1/transfer"


def payout_settings(services: Services, payout_user: str = "mattf") -> Settings:
    return Settings(
        session_secret=services.settings.session_secret,
        database_url=services.settings.database_url,
        base_url=services.settings.base_url,
        beans_url=services.settings.beans_url,
        beans_token="beans-token",
        beans_payout_user=payout_user,
    )


@respx.mock
async def test_payout_sends_the_whole_wallet_to_the_payout_account(services: Services) -> None:
    respx.get(WALLET_URL).respond(json={"username": "workflows", "bean_amount": 42})
    transfer = respx.post(TRANSFER_URL).respond(json={"message": "transfer successful"})
    payout = build_payout(services.http, payout_settings(services))
    assert payout is not None

    assert await payout.send_all() == 42

    request = transfer.calls.last.request
    assert json.loads(request.content) == {"to_user": "mattf", "amount": 42, "force": False}
    assert request.headers["Authorization"] == "Bearer beans-token"


@respx.mock
async def test_payout_of_an_empty_wallet_sends_nothing(services: Services) -> None:
    respx.get(WALLET_URL).respond(json={"username": "workflows", "bean_amount": 0})
    transfer = respx.post(TRANSFER_URL)
    payout = BeansPayout(services.http, payout_settings(services))

    with pytest.raises(PayoutError, match="empty"):
        await payout.send_all()
    assert not transfer.called


@respx.mock
async def test_payout_reports_why_beans_refused(services: Services) -> None:
    respx.get(WALLET_URL).respond(json={"username": "workflows", "bean_amount": 5})
    respx.post(TRANSFER_URL).respond(
        400, json={"error": "recipient not found, use force=true to create wallet"}
    )
    payout = BeansPayout(services.http, payout_settings(services))

    with pytest.raises(PayoutError, match="recipient not found"):
        await payout.send_all()


def test_payout_is_off_without_a_payout_account(services: Services) -> None:
    assert build_payout(services.http, payout_settings(services, payout_user="")) is None
