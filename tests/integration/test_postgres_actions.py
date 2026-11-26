"""Optional production-dialect checks in an isolated schema of a test database."""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.repositories.db import init_schema
from app.repositories.models import RefundRow
from app.services.action_service import ActionService
from app.services.seed import seed_demo_data
from tests.integration.test_actions import confirm, propose


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("ACTION_TEST_POSTGRES_URL"),
    reason="Dedicated PostgreSQL test URL not supplied",
)
async def test_postgres_independent_connections_create_one_refund():
    """The opt-in database account must permit creating a disposable schema."""
    url = os.environ["ACTION_TEST_POSTGRES_URL"]
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("ACTION_TEST_POSTGRES_URL must use postgresql+asyncpg")
    schema = "action_test_" + uuid.uuid4().hex
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as db:
            await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        await init_schema(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory.begin() as db:
            await seed_demo_data(db)
        service = ActionService(factory)
        await propose(service, action_id="a")
        await propose(service, action_id="b")
        results = await asyncio.gather(
            confirm(service, "a"), confirm(service, "b"), confirm(service, "a")
        )
        assert any(r.status.value == "SUCCEEDED" for r in results)
        async with factory() as db:
            assert await db.scalar(select(func.count()).select_from(RefundRow)) == 1
    finally:
        async with engine.begin() as db:
            await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
