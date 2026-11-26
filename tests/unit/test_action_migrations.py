"""Upgrade preserves existing business rows; downgrade removes only new tables."""

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic import command
from alembic.config import Config


@pytest.mark.parametrize("migration_driver", ["sqlite", "sqlite+aiosqlite"])
def test_upgrade_and_downgrade_preserve_existing_order(tmp_path, migration_driver):
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.replace("sqlite:", migration_driver + ":"))
    command.upgrade(config, "0002")
    engine = create_engine(url)
    with engine.begin() as db:
        db.execute(
            text(
                "INSERT INTO orders(order_id,user_id,status,total_cents,created_at) VALUES ('OLD','u','paid',100,CURRENT_TIMESTAMP)"
            )
        )
    command.upgrade(config, "head")
    assert {"operations", "operation_events", "order_reservations"} <= set(
        inspect(engine).get_table_names()
    )
    with engine.connect() as db:
        assert db.scalar(text("SELECT total_cents FROM orders WHERE order_id='OLD'")) == 100
    command.downgrade(config, "0002")
    assert "operations" not in inspect(engine).get_table_names()
    assert "returns" in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    engine.dispose()
