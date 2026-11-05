import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)


def include_object(obj, name, type_, reflected, compare_to):
    # LangGraph owns its checkpoint tables (created by AsyncPostgresSaver.setup()); never autogenerate drops for them.
    if type_ == "table" and name.startswith("checkpoint"):
        return False
    if type_ == "index" and getattr(getattr(obj, "table", None), "name", "").startswith("checkpoint"):
        return False
    return True


def do_migrations(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


async def run() -> None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        await conn.run_sync(do_migrations)
    await engine.dispose()


asyncio.run(run())
