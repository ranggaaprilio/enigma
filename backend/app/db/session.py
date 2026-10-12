from typing import Any
from uuid import uuid4

from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.core.metrics import DbPoolCollector


def engine_options(settings: Settings) -> dict[str, Any]:
    if settings.db_pgbouncer:
        # SQLAlchemy's documented asyncpg-behind-PgBouncer setup: PgBouncer owns the pool, and
        # a server connection can serve a different client after every transaction, so nothing
        # prepared may be cached or reuse a predictable name.
        return {
            "poolclass": NullPool,
            "connect_args": {
                "statement_cache_size": 0,
                "prepared_statement_cache_size": 0,
                "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
            },
        }
    return {
        "pool_pre_ping": True,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        "pool_recycle": settings.db_pool_recycle,
    }


settings = get_settings()

engine = create_async_engine(settings.database_url, **engine_options(settings))
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)
# Epic 09: pool occupancy on /metrics (API and workers share this module).
REGISTRY.register(DbPoolCollector(engine))


async def get_db_session():
    async with async_session_maker() as session:
        yield session
