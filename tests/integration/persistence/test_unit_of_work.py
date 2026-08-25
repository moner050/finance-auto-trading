from __future__ import annotations

from collections.abc import Callable

import pytest

from autotrader.application.ports import AsyncUnitOfWork
from autotrader.persistence.mysql.unit_of_work import SqlAlchemyUnitOfWork


class FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False
        self.closed = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def close(self) -> None:
        self.closed = True


def session_factory(session: FakeSession) -> Callable[[], FakeSession]:
    return lambda: session


@pytest.mark.asyncio
async def test_unit_of_work_commits_and_closes_on_success() -> None:
    session = FakeSession()
    uow = SqlAlchemyUnitOfWork(session_factory(session))

    assert isinstance(uow, AsyncUnitOfWork)
    async with uow:
        assert uow.session is session

    assert session.committed is True
    assert session.rolled_back is False
    assert session.closed is True


@pytest.mark.asyncio
async def test_unit_of_work_rolls_back_and_closes_on_error() -> None:
    session = FakeSession()
    uow = SqlAlchemyUnitOfWork(session_factory(session))

    with pytest.raises(RuntimeError, match="boom"):
        async with uow:
            raise RuntimeError("boom")

    assert session.committed is False
    assert session.rolled_back is True
    assert session.closed is True
