"""Request-scoped dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import db as core_db


async def db_session() -> AsyncIterator[AsyncSession]:
    """One session per request.

    Resolved through the module rather than a from-import so there is a single
    place to point at a different database.
    """
    async with core_db.get_sessionmaker()() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(db_session)]
