import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.environ.get("DATABASE_URL")
if database_url is None:
    raise ValueError("DATABASE_URL environment variable is required for migrations")

# asyncpg:// -> postgresql:// for synchronous alembic driver
sync_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
if sync_url.startswith("postgresql://"):
    pass
else:
    sync_url = database_url


def run_migrations_offline():
    context.configure(url=sync_url, target_metadata=None, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = create_engine(sync_url)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
