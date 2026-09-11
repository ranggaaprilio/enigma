from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, Comment, PullRequest, SpecPhase, SpecWorkflow, Workspace
from app.models.comment import COMMENT_TARGET_TYPES
from app.services.access import require_workspace_access
from app.services.notifications import NotificationService

logger = structlog.get_logger("app.comments")


class InvalidCommentTargetError(Exception):
    pass


class CommentNotFoundError(Exception):
    pass


class CommentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _resolve_target_workspace_id(self, target_type: str, target_id: UUID) -> UUID:
        if target_type not in COMMENT_TARGET_TYPES:
            raise InvalidCommentTargetError
        if target_type == "run":
            run = await self.session.get(AgentRun, target_id)
            if run is None:
                raise InvalidCommentTargetError
            return run.workspace_id
        if target_type == "spec_phase":
            phase = await self.session.get(SpecPhase, target_id)
            if phase is None:
                raise InvalidCommentTargetError
            workflow = await self.session.get(SpecWorkflow, phase.workflow_id)
            if workflow is None:
                raise InvalidCommentTargetError
            return workflow.workspace_id
        pull_request = await self.session.get(PullRequest, target_id)
        if pull_request is None:
            raise InvalidCommentTargetError
        return pull_request.workspace_id

    async def _notify_recipients(
        self, target_type: str, target_id: UUID, workspace: Workspace, comment: Comment
    ) -> None:
        recipient_ids: set[UUID] = set()
        if target_type == "run":
            run = await self.session.get(AgentRun, target_id)
            if run is not None:
                recipient_ids.add(run.user_id)
        elif target_type == "spec_phase":
            phase = await self.session.get(SpecPhase, target_id)
            if phase is not None:
                if phase.reviewed_by is not None:
                    recipient_ids.add(phase.reviewed_by)
                workflow = await self.session.get(SpecWorkflow, phase.workflow_id)
                if workflow is not None:
                    recipient_ids.add(workflow.user_id)
        recipient_ids.add(workspace.owner_id)
        recipient_ids.discard(comment.user_id)

        notification_service = NotificationService(self.session)
        snippet = comment.body if len(comment.body) <= 140 else f"{comment.body[:140]}..."
        for recipient_id in recipient_ids:
            await notification_service.create(
                user_id=recipient_id,
                workspace_id=workspace.id,
                notification_type="comment_created",
                title=f"New comment on {target_type.replace('_', ' ')}",
                message=snippet,
            )

    async def add(
        self, target_type: str, target_id: UUID, user_id: UUID, body: str
    ) -> Comment:
        workspace_id = await self._resolve_target_workspace_id(target_type, target_id)
        workspace, _role = await require_workspace_access(self.session, workspace_id, user_id)

        comment = Comment(
            target_type=target_type,
            target_id=target_id,
            user_id=user_id,
            body=body,
        )
        self.session.add(comment)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(comment)

        # Best-effort: the comment is already committed, so a notification failure must not
        # turn a successful post into a 500 the client would retry (double-posting).
        try:
            await self._notify_recipients(target_type, target_id, workspace, comment)
        except Exception:
            logger.exception("comment.notify_failed", comment_id=str(comment.id))

        return comment

    async def list_for_target(
        self, target_type: str, target_id: UUID, user_id: UUID
    ) -> list[Comment]:
        workspace_id = await self._resolve_target_workspace_id(target_type, target_id)
        await require_workspace_access(self.session, workspace_id, user_id)

        result = await self.session.execute(
            select(Comment)
            .where(Comment.target_type == target_type, Comment.target_id == target_id)
            .order_by(Comment.created_at.asc())
        )
        return list(result.scalars().all())

    async def resolve(self, comment_id: UUID, user_id: UUID) -> Comment:
        comment = await self.session.get(Comment, comment_id)
        if comment is None:
            raise CommentNotFoundError
        workspace_id = await self._resolve_target_workspace_id(
            comment.target_type, comment.target_id
        )
        await require_workspace_access(self.session, workspace_id, user_id)

        if comment.resolved_at is None:
            comment.resolved_at = datetime.now(UTC)
            comment.resolved_by = user_id
            self.session.add(comment)
            await self.session.commit()
            await self.session.refresh(comment)
        return comment
