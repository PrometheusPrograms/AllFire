import sqlite3
from datetime import UTC, datetime
from logging.config import fileConfig

from sqlalchemy import engine_from_config, event, pool

from alembic import context
from app.config import settings
from app.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use the app's own settings rather than a hardcoded alembic.ini URL.
config.set_main_option("sqlalchemy.url", settings.sqlalchemy_database_url)

# Models' metadata, for 'autogenerate' support.
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # See app/db.py — same dev-only shim so `now()` server_defaults don't
    # blow up when migrations are dry-run against SQLite locally.
    if connectable.dialect.name == "sqlite":

        @event.listens_for(connectable, "connect")
        def _register_sqlite_now(dbapi_connection: sqlite3.Connection, _) -> None:
            dbapi_connection.create_function(
                "now", 0, lambda: datetime.now(UTC).isoformat(sep=" ")
            )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
