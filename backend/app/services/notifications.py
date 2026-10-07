import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification
from app.services.runtime import EventBus, InMemoryEventBus, event_bus


class NotificationNotFoundError(Exception):
    pass


class NotificationRuntime:
    """Per-user notification fan-out over the shared ``EventBus``."""

    def __init__(self, bus: EventBus | None = None) -> None:
        self.bus = bus if bus is not None else InMemoryEventBus()

    @staticmethod
    def _topic(user_id: UUID) -> str:
        return f"notifications:{user_id}"

    def subscribe(self, user_id: UUID) -> asyncio.Queue[dict]:
        return self.bus.subscribe(self._topic(user_id))

    def unsubscribe(self, user_id: UUID, queue: asyncio.Queue[dict]) -> None:
        self.bus.unsubscribe(self._topic(user_id), queue)

    async def publish(self, user_id: UUID, payload: dict) -> None:
        await self.bus.publish(self._topic(user_id), payload)


notification_runtime = NotificationRuntime(event_bus)


class NotificationService:
    def __init__(
        self,
        session: AsyncSession,
        runtime: NotificationRuntime = notification_runtime,
    ) -> None:
        self.session = session
        self.runtime = runtime

    async def create(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        notification_type: str,
        title: str,
        message: str,
    ) -> Notification:
        notification = Notification(
            user_id=user_id,
            workspace_id=workspace_id,
            notification_type=notification_type,
            title=title,
            message=message,
        )
        self.session.add(notification)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(notification)
        await self.runtime.publish(user_id, notification_payload(notification))
        return notification

    async def list_for_user(self, user_id: UUID, limit: int = 50) -> list[Notification]:
        result = await self.session.execute(
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def mark_read(self, notification_id: UUID, user_id: UUID) -> Notification:
        result = await self.session.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
            )
        )
        notification = result.scalar_one_or_none()
        if notification is None:
            raise NotificationNotFoundError
        if notification.read_at is None:
            notification.read_at = datetime.now(UTC)
            self.session.add(notification)
            await self.session.commit()
            await self.session.refresh(notification)
        return notification

    async def mark_all_read(self, user_id: UUID) -> list[Notification]:
        notifications = await self.list_for_user(user_id)
        now = datetime.now(UTC)
        changed = False
        for notification in notifications:
            if notification.read_at is None:
                notification.read_at = now
                self.session.add(notification)
                changed = True
        if changed:
            await self.session.commit()
            for notification in notifications:
                await self.session.refresh(notification)
        return notifications


def notification_payload(notification: Notification) -> dict:
    return {
        "id": str(notification.id),
        "user_id": str(notification.user_id),
        "workspace_id": str(notification.workspace_id),
        "type": notification.notification_type,
        "title": notification.title,
        "message": notification.message,
        "read_at": notification.read_at.isoformat() if notification.read_at else None,
        "created_at": notification.created_at.isoformat(),
    }
