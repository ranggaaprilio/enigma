"""Shared workspace-response wiring for the API layer.

`WorkspaceResponse` carries `viewer_role` — the requesting user's role *in that workspace* —
so clients can hide or disable actions they are not allowed to perform. Resolving it needs
both the workspace row and the caller, which the ORM object alone cannot provide, so every
endpoint returning a workspace builds its response through here.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Workspace
from app.schemas.workspaces import WorkspaceResponse
from app.services.access import resolve_workspace_role


async def workspace_response(
    session: AsyncSession, workspace: Workspace, user_id: UUID
) -> WorkspaceResponse:
    role = await resolve_workspace_role(session, workspace, user_id)
    return WorkspaceResponse.model_validate(workspace).model_copy(
        update={"viewer_role": role.value if role is not None else None}
    )


async def workspace_responses(
    session: AsyncSession, workspaces: list[Workspace], user_id: UUID
) -> list[WorkspaceResponse]:
    return [await workspace_response(session, workspace, user_id) for workspace in workspaces]
