from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, Project, ProjectMember, Team, User, Workspace
from app.schemas.usage import (
    UsageBreakdownItem,
    UsageScope,
    UsageSeriesPoint,
    UsageSummaryResponse,
    UsageTotals,
)

TOP_SPENDERS_LIMIT = 5


class UsageAccessError(Exception):
    """Raised when the caller may not read usage for the requested scope."""


class UsageScopeError(Exception):
    """Raised when the scope/scope_id combination is invalid."""


class SqlAlchemyUsageService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def usage_summary(
        self,
        user_id: UUID,
        scope: UsageScope | None,
        scope_id: UUID | None,
        date_from: datetime,
        date_to: datetime,
    ) -> UsageSummaryResponse:
        project_ids, team_ids = await self._accessible_scope(user_id)

        # Workspaces the user can access: owned by them OR in an accessible team.
        workspace_filter = Workspace.owner_id == user_id
        if team_ids:
            workspace_filter = or_(workspace_filter, Workspace.team_id.in_(team_ids))

        if scope is not None:
            if scope_id is None:
                raise UsageScopeError("scope_id is required when scope is provided")
            await self._assert_scope_access(scope, scope_id, user_id, project_ids, team_ids)

        time_filter = AgentRun.started_at.between(date_from, date_to)

        totals = await self._totals(scope, scope_id, workspace_filter, time_filter)
        series = await self._series(scope, scope_id, workspace_filter, time_filter)
        dimension, breakdown = await self._breakdown(
            scope, scope_id, workspace_filter, time_filter
        )
        top_spenders = await self._top_spenders(scope, scope_id, workspace_filter, time_filter)

        return UsageSummaryResponse(
            scope=scope,
            scope_id=scope_id,
            date_from=date_from,
            date_to=date_to,
            totals=totals,
            series=series,
            breakdown_dimension=dimension,
            breakdown=breakdown,
            top_spenders=top_spenders,
        )

    # ------------------------------------------------------------------
    # Access scoping (mirrors services/dashboard.py membership rules)
    # ------------------------------------------------------------------

    async def _accessible_scope(self, user_id: UUID) -> tuple[list[UUID], list[UUID]]:
        project_ids = list(
            (
                await self.session.execute(
                    select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        team_ids: list[UUID] = []
        if project_ids:
            team_ids = list(
                (
                    await self.session.execute(
                        select(Team.id).where(Team.project_id.in_(project_ids))
                    )
                )
                .scalars()
                .all()
            )
        return project_ids, team_ids

    async def _assert_scope_access(
        self,
        scope: UsageScope,
        scope_id: UUID,
        user_id: UUID,
        project_ids: list[UUID],
        team_ids: list[UUID],
    ) -> None:
        if scope == "project":
            if scope_id not in project_ids:
                raise UsageAccessError("No access to this project")
        elif scope == "team":
            team = await self.session.get(Team, scope_id)
            if team is None or team.project_id not in project_ids:
                raise UsageAccessError("No access to this team")
        elif scope == "workspace":
            workspace = await self.session.get(Workspace, scope_id)
            if workspace is None or (
                workspace.owner_id != user_id and workspace.team_id not in team_ids
            ):
                raise UsageAccessError("No access to this workspace")
        else:  # pragma: no cover - guarded by schema Literal
            raise UsageScopeError(f"Unknown scope: {scope}")

    def _scope_join_filter(self, stmt, scope, scope_id, workspace_filter):
        """Apply scope joins + filter to a statement selecting from AgentRun."""
        if scope == "workspace":
            return stmt.where(AgentRun.workspace_id == scope_id)
        stmt = stmt.join(Workspace, Workspace.id == AgentRun.workspace_id)
        if scope == "team":
            return stmt.where(Workspace.team_id == scope_id)
        if scope == "project":
            return stmt.join(Team, Team.id == Workspace.team_id).where(
                Team.project_id == scope_id
            )
        return stmt.where(workspace_filter)

    # ------------------------------------------------------------------
    # Aggregations
    # ------------------------------------------------------------------

    async def _totals(self, scope, scope_id, workspace_filter, time_filter) -> UsageTotals:
        stmt = self._scope_join_filter(
            select(
                func.count(AgentRun.id),
                func.coalesce(func.sum(AgentRun.input_tokens), 0),
                func.coalesce(func.sum(AgentRun.output_tokens), 0),
                func.coalesce(func.sum(AgentRun.tokens_used), 0),
                func.coalesce(func.sum(AgentRun.estimated_cost), 0),
            ).where(time_filter),
            scope,
            scope_id,
            workspace_filter,
        )
        row = (await self.session.execute(stmt)).one()
        return UsageTotals(
            run_count=int(row[0]),
            input_tokens=int(row[1]),
            output_tokens=int(row[2]),
            tokens_used=int(row[3]),
            estimated_cost=float(row[4]),
        )

    async def _series(
        self, scope, scope_id, workspace_filter, time_filter
    ) -> list[UsageSeriesPoint]:
        bucket = func.date_trunc("day", AgentRun.started_at)
        stmt = self._scope_join_filter(
            select(
                bucket.label("bucket"),
                func.count(AgentRun.id),
                func.coalesce(func.sum(AgentRun.tokens_used), 0),
                func.coalesce(func.sum(AgentRun.estimated_cost), 0),
            ).where(time_filter),
            scope,
            scope_id,
            workspace_filter,
        ).group_by(bucket).order_by(bucket)
        rows = (await self.session.execute(stmt)).all()
        return [
            UsageSeriesPoint(
                date=row[0],
                run_count=int(row[1]),
                tokens_used=int(row[2]),
                estimated_cost=float(row[3]),
            )
            for row in rows
        ]

    async def _breakdown(
        self, scope, scope_id, workspace_filter, time_filter
    ) -> tuple[str, list[UsageBreakdownItem]]:
        count = func.count(AgentRun.id)
        tokens = func.coalesce(func.sum(AgentRun.tokens_used), 0)
        cost = func.coalesce(func.sum(AgentRun.estimated_cost), 0)

        if scope == "workspace":
            dimension = "user"
            stmt = (
                select(AgentRun.user_id, User.email, count, tokens, cost)
                .join(User, User.id == AgentRun.user_id)
                .where(time_filter, AgentRun.workspace_id == scope_id)
                .group_by(AgentRun.user_id, User.email)
            )
        elif scope == "team":
            dimension = "workspace"
            stmt = (
                select(Workspace.id, Workspace.name, count, tokens, cost)
                .join(Workspace, Workspace.id == AgentRun.workspace_id)
                .where(time_filter, Workspace.team_id == scope_id)
                .group_by(Workspace.id, Workspace.name)
            )
        elif scope == "project":
            dimension = "team"
            stmt = (
                select(Team.id, Team.name, count, tokens, cost)
                .join(Workspace, Workspace.id == AgentRun.workspace_id)
                .join(Team, Team.id == Workspace.team_id)
                .where(time_filter, Team.project_id == scope_id)
                .group_by(Team.id, Team.name)
            )
        else:
            dimension = "project"
            stmt = (
                select(Project.id, Project.name, count, tokens, cost)
                .join(Workspace, Workspace.id == AgentRun.workspace_id)
                .join(Team, Team.id == Workspace.team_id)
                .join(Project, Project.id == Team.project_id)
                .where(time_filter, workspace_filter)
                .group_by(Project.id, Project.name)
            )

        stmt = stmt.order_by(cost.desc())
        rows = (await self.session.execute(stmt)).all()
        items = [
            UsageBreakdownItem(
                id=row[0],
                name=str(row[1]),
                run_count=int(row[2]),
                tokens_used=int(row[3]),
                estimated_cost=float(row[4]),
            )
            for row in rows
        ]
        return dimension, items

    async def _top_spenders(
        self, scope, scope_id, workspace_filter, time_filter
    ) -> list[UsageBreakdownItem]:
        count = func.count(AgentRun.id)
        tokens = func.coalesce(func.sum(AgentRun.tokens_used), 0)
        cost = func.coalesce(func.sum(AgentRun.estimated_cost), 0)
        stmt = self._scope_join_filter(
            select(AgentRun.user_id, User.email, count, tokens, cost)
            .join(User, User.id == AgentRun.user_id)
            .where(time_filter),
            scope,
            scope_id,
            workspace_filter,
        )
        stmt = (
            stmt.group_by(AgentRun.user_id, User.email)
            .order_by(cost.desc())
            .limit(TOP_SPENDERS_LIMIT)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            UsageBreakdownItem(
                id=row[0],
                name=str(row[1]),
                run_count=int(row[2]),
                tokens_used=int(row[3]),
                estimated_cost=float(row[4]),
            )
            for row in rows
        ]
