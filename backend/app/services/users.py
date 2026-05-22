from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


class DuplicateUserEmailError(Exception):
    pass


class SqlAlchemyUserStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def register_user(self, email: str, name: str | None, password_hash: str) -> User:
        async with self.session.begin():
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": 427001},
            )

            existing_user = await self.get_by_email(email)
            if existing_user is not None:
                raise DuplicateUserEmailError

            result = await self.session.execute(select(func.count()).select_from(User))
            role = "owner" if result.scalar_one() == 0 else "viewer"
            user = User(email=email, name=name, password_hash=password_hash, role=role)
            self.session.add(user)
            await self.session.flush()

        return user
