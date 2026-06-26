from collections import defaultdict
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectMember, Team
from app.models.workspace import Workspace
from app.schemas.dashboard import (
    DashboardProjectItem,
    DashboardSummaryResponse,
    DashboardWorkspaceItem,
    WorkspaceStatusCounts,
)

RECENT_LIMIT = 5


class SqlAlchemyDashboardService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def summary(self, user_id: UUID) -> DashboardSummaryResponse:
        # Projects the user can access (those they are a member of).
        project_ids_result = await self.session.execute(
            select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
        )
        accessible_project_ids = list(project_ids_result.scalars().all())
        projects_total = len(accessible_project_ids)

        # Teams in accessible projects (project members see all teams in their projects).
        accessible_team_ids: list[UUID] = []
        if accessible_project_ids:
            team_ids_result = await self.session.execute(
                select(Team.id).where(Team.project_id.in_(accessible_project_ids))
            )
            accessible_team_ids = list(team_ids_result.scalars().all())
        teams_total = len(accessible_team_ids)

        # Workspaces the user can access: owned by them OR belonging to an accessible team.
        workspace_filter = Workspace.owner_id == user_id
        if accessible_team_ids:
            workspace_filter = or_(workspace_filter, Workspace.team_id.in_(accessible_team_ids))

        # Total + status breakdown in a single grouped query.
        status_result = await self.session.execute(
            select(Workspace.status, func.count(Workspace.id))
            .where(workspace_filter)
            .group_by(Workspace.status)
        )
        status_counts: dict[str, int] = {}
        workspaces_total = 0
        for status_value, count in status_result.all():
            status_counts[status_value] = count
            workspaces_total += count

        # Recent projects (top 5) with per-project team and workspace counts.
        recent_projects: list[DashboardProjectItem] = []
        if accessible_project_ids:
            projects_result = await self.session.execute(
                select(Project)
                .where(Project.id.in_(accessible_project_ids))
                .order_by(Project.created_at.desc())
                .limit(RECENT_LIMIT)
            )
            top_projects = list(projects_result.scalars().all())
            top_project_ids = [p.id for p in top_projects]

            team_count_map: dict[UUID, int] = defaultdict(int)
            workspace_count_map: dict[UUID, int] = defaultdict(int)
            if top_project_ids:
                tc_result = await self.session.execute(
                    select(Team.project_id, func.count(Team.id))
                    .where(Team.project_id.in_(top_project_ids))
                    .group_by(Team.project_id)
                )
                for project_id, count in tc_result.all():
                    team_count_map[project_id] = count

                wc_result = await self.session.execute(
                    select(Team.project_id, func.count(Workspace.id))
                    .join(Workspace, Workspace.team_id == Team.id)
                    .where(Team.project_id.in_(top_project_ids))
                    .group_by(Team.project_id)
                )
                for project_id, count in wc_result.all():
                    workspace_count_map[project_id] = count

            recent_projects = [
                DashboardProjectItem(
                    id=p.id,
                    name=p.name,
                    slug=p.slug,
                    description=p.description,
                    team_count=team_count_map.get(p.id, 0),
                    workspace_count=workspace_count_map.get(p.id, 0),
                    created_at=p.created_at,
                )
                for p in top_projects
            ]

        # Recent workspaces (top 5) with team -> project context for navigation.
        ws_result = await self.session.execute(
            select(Workspace, Team, Project)
            .outerjoin(Team, Workspace.team_id == Team.id)
            .outerjoin(Project, Team.project_id == Project.id)
            .where(workspace_filter)
            .order_by(Workspace.created_at.desc())
            .limit(RECENT_LIMIT)
        )
        recent_workspaces = [
            DashboardWorkspaceItem(
                id=ws.id,
                name=ws.name,
                status=ws.status,
                project_id=project.id if project is not None else None,
                project_name=project.name if project is not None else None,
                team_id=team.id if team is not None else None,
                team_name=team.name if team is not None else None,
                created_at=ws.created_at,
            )
            for ws, team, project in ws_result.all()
        ]

        return DashboardSummaryResponse(
            projects_total=projects_total,
            teams_total=teams_total,
            workspaces_total=workspaces_total,
            workspaces_by_status=WorkspaceStatusCounts(**status_counts),
            recent_projects=recent_projects,
            recent_workspaces=recent_workspaces,
        )
