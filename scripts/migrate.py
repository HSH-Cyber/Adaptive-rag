"""Run Alembic using the application's configured async database URL."""

import argparse
from pathlib import Path

from sqlalchemy.engine import make_url

from alembic import command
from alembic.config import Config
from app.config.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision", nargs="?", default="head")
    args = parser.parse_args()
    settings = get_settings()
    database = make_url(settings.database_url).database
    if settings.use_sqlite and database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(config, args.revision)


if __name__ == "__main__":
    main()
