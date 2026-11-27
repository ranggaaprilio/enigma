"""This database's sandbox installation id (``sandbox_installation``).

Providers label every sandbox they create with it, and upkeep only ever touches sandboxes
carrying their own installation's id, so two installations sharing a daemon or an
OpenSandbox server never sweep each other's.
"""

from __future__ import annotations

from app.core.config import Settings
from app.services.sandbox.base import RuntimeUnavailableError

INSTALLATION_LABEL = "enigma.installation"


async def load_installation_id(settings: Settings) -> str:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.models import SandboxInstallation

    # A throwaway connection: a pooled one would be bound to whichever event loop opened it.
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(select(SandboxInstallation.id))
    finally:
        await engine.dispose()
    if value is None:
        raise RuntimeUnavailableError(
            "This database has no sandbox installation id; run the migrations"
        )
    return str(value)
