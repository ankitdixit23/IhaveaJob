"""Alembic environment. The engine is passed in programmatically by
``ihaveajob.database.upgrade`` (or built from DATABASE_URL for the alembic CLI)."""

from __future__ import annotations

import os

from alembic import context

from ihaveajob.database.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = config.attributes.get("engine")
    if engine is None:
        from ihaveajob.database import make_engine

        engine = make_engine(os.getenv("DATABASE_URL", "sqlite:///data/ihaveajob.db"))
    with engine.begin() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
