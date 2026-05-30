from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.core.security import verify_session_token
from app.db.session import async_session_maker, get_db_session
from app.models import AuditLog, Notification, User
from app.schemas.notifications import AuditLogResponse, NotificationResponse
from app.services.audit import AuditLogService
from app.services.notifications import (
    NotificationNotFoundError,
    NotificationService,
    notification_runtime,
)
from app.services.workspaces import WorkspaceNotFoundError

router = APIRouter(tags=["notifications"])

DB_SESSION_DEP = Depends(get_db_session)


async def get_notification_service(session: AsyncSession = DB_SESSION_DEP) -> NotificationService:
    return NotificationService(session=session)


async def get_audit_log_service(session: AsyncSession = DB_SESSION_DEP) -> AuditLogService:
    return AuditLogService(session=session)


NOTIFICATION_SERVICE_DEP = Depends(get_notification_service)
AUDIT_LOG_SERVICE_DEP = Depends(get_audit_log_service)


def notification_response(notification: Notification) -> NotificationResponse:
    return NotificationResponse(
        id=notification.id,
        user_id=notification.user_id,
        workspace_id=notification.workspace_id,
        type=notification.notification_type,
        title=notification.title,
        message=notification.message,
        read_at=notification.read_at,
        created_at=notification.created_at,
    )


def audit_log_response(audit_log: AuditLog) -> AuditLogResponse:
    return AuditLogResponse(
        id=audit_log.id,
        user_id=audit_log.user_id,
        workspace_id=audit_log.workspace_id,
        action=audit_log.action,
        target_type=audit_log.target_type,
        target_id=audit_log.target_id,
        metadata=audit_log.log_metadata,
        created_at=audit_log.created_at,
    )


@router.get("/notifications", response_model=list[NotificationResponse])
async def list_notifications(
    current_user: User = CURRENT_USER_DEP,
    service: NotificationService = NOTIFICATION_SERVICE_DEP,
) -> list[NotificationResponse]:
    notifications = await service.list_for_user(current_user.id)
    return [notification_response(notification) for notification in notifications]


@router.patch("/notifications/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_read(
    notification_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: NotificationService = NOTIFICATION_SERVICE_DEP,
) -> NotificationResponse:
    try:
        notification = await service.mark_read(notification_id, current_user.id)
    except NotificationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        ) from None
    return notification_response(notification)


@router.post("/notifications/read-all", response_model=list[NotificationResponse])
async def mark_all_notifications_read(
    current_user: User = CURRENT_USER_DEP,
    service: NotificationService = NOTIFICATION_SERVICE_DEP,
) -> list[NotificationResponse]:
    notifications = await service.mark_all_read(current_user.id)
    return [notification_response(notification) for notification in notifications]


@router.get("/workspaces/{workspace_id}/audit-logs", response_model=list[AuditLogResponse])
async def list_audit_logs(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> list[AuditLogResponse]:
    try:
        audit_logs = await service.list_for_workspace(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        ) from None
    return [audit_log_response(audit_log) for audit_log in audit_logs]


@router.websocket("/notifications/stream")
async def stream_notifications(websocket: WebSocket) -> None:
    settings = get_settings()
    async with async_session_maker() as session:
        user = await websocket_user(websocket, session, settings)
        if user is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        user_id = user.id

    await websocket.accept()
    queue = notification_runtime.subscribe(user_id)
    try:
        while True:
            await websocket.send_json(await queue.get())
    except WebSocketDisconnect:
        return
    finally:
        notification_runtime.unsubscribe(user_id, queue)


async def websocket_user(
    websocket: WebSocket,
    session: AsyncSession,
    settings: Settings,
) -> User | None:
    token = websocket.cookies.get(settings.auth_cookie_name)
    if token is None:
        return None
    payload = verify_session_token(token, settings.auth_secret_key)
    if payload is None:
        return None
    try:
        user_id = UUID(str(payload["sub"]))
    except (KeyError, ValueError):
        return None
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user
