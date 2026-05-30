import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification


class NotificationNotFoundError(Exception):
    pass


class NotificationRuntime:
    def __init__(self) -> None:
        self.subscribers: dict[UUID, set[asyncio.Queue[dict]]] = defaultdict(set)

    def subscribe(self, user_id: UUID) -> asyncio.Queue[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self.subscribers[user_id].add(queue)
        return queue

    def unsubscribe(self, user_id: UUID, queue: asyncio.Queue[dict]) -> None:
        queues = self.subscribers.get(user_id)
        if not queues:
            return
        queues.discard(queue)
        if not queues:
            self.subscribers.pop(user_id, None)

    async def publish(self, user_id: UUID, payload: dict) -> None:
        for queue in list(self.subscribers.get(user_id, set())):
            await queue.put(payload)


notification_runtime = NotificationRuntime()


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
