"""Alembic environment.

Runs synchronously: SQLAlchemy's psycopg 3 dialect serves both sync and async use from
the same URL, so migrations need no event loop.
"""

from sqlalchemy import create_engine, pool

from alembic import context
from app.config import get_settings
from app.models import Base

target_metadata = Base.metadata


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    # Procrastinate manages its own tables; keep them out of autogenerate.
    return not (type_ == "table" and name is not None and name.startswith("procrastinate_"))


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().sqlalchemy_url,
        target_metadata=target_metadata,
        include_name=include_name,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_settings().sqlalchemy_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_name=include_name,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
