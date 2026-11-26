"""File-backed databases for durable action concurrency tests."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.repositories.db import init_schema
from app.services.seed import seed_demo_data


@pytest.fixture
async def actions_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'actions.db'}")
    await init_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as db:
        await seed_demo_data(db)
    yield factory
    await engine.dispose()
