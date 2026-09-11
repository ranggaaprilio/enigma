from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.db.session import get_db_session
from app.models import Comment, User
from app.schemas.comments import CommentCreateRequest, CommentResponse, CommentTargetType
from app.services.access import WorkspaceAccessDeniedError
from app.services.comments import CommentNotFoundError, CommentService, InvalidCommentTargetError
from app.services.workspaces import WorkspaceNotFoundError

router = APIRouter(prefix="/comments", tags=["comments"])

DB_SESSION_DEP = Depends(get_db_session)


async def get_comment_service(session: AsyncSession = DB_SESSION_DEP) -> CommentService:
    return CommentService(session=session)


COMMENT_SERVICE_DEP = Depends(get_comment_service)


def comment_response(comment: Comment) -> CommentResponse:
    return CommentResponse(
        id=comment.id,
        target_type=comment.target_type,
        target_id=comment.target_id,
        user_id=comment.user_id,
        body=comment.body,
        resolved_at=comment.resolved_at,
        resolved_by=comment.resolved_by,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
    )


def _invalid_target() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment target not found")


def _access_denied() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Workspace not found",
    )


@router.post("", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
async def create_comment(
    payload: CommentCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    service: CommentService = COMMENT_SERVICE_DEP,
) -> CommentResponse:
    try:
        comment = await service.add(
            payload.target_type, payload.target_id, current_user.id, payload.body
        )
    except InvalidCommentTargetError:
        raise _invalid_target() from None
    except (WorkspaceNotFoundError, WorkspaceAccessDeniedError):
        raise _access_denied() from None
    return comment_response(comment)


@router.get("", response_model=list[CommentResponse])
async def list_comments(
    target_type: CommentTargetType = Query(...),
    target_id: UUID = Query(...),
    current_user: User = CURRENT_USER_DEP,
    service: CommentService = COMMENT_SERVICE_DEP,
) -> list[CommentResponse]:
    try:
        comments = await service.list_for_target(target_type, target_id, current_user.id)
    except InvalidCommentTargetError:
        raise _invalid_target() from None
    except (WorkspaceNotFoundError, WorkspaceAccessDeniedError):
        raise _access_denied() from None
    return [comment_response(comment) for comment in comments]


@router.patch("/{comment_id}/resolve", response_model=CommentResponse)
async def resolve_comment(
    comment_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: CommentService = COMMENT_SERVICE_DEP,
) -> CommentResponse:
    try:
        comment = await service.resolve(comment_id, current_user.id)
    except CommentNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found"
        ) from None
    except InvalidCommentTargetError:
        raise _invalid_target() from None
    except (WorkspaceNotFoundError, WorkspaceAccessDeniedError):
        raise _access_denied() from None
    return comment_response(comment)
