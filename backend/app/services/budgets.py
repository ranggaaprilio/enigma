from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AgentRun,
    ProjectMember,
    Team,
    TeamMember,
    UsageBudget,
    Workspace,
)
from app.schemas.budgets import BudgetScope
from app.services.audit import AuditLogService
from app.services.notifications import NotificationService


class BudgetAccessError(Exception):
    """Caller lacks the role required to read or manage the scope's budgets."""


class BudgetNotFoundError(Exception):
    pass


class BudgetConflictError(Exception):
    """A budget already exists for this (scope, scope_id, period)."""


class BudgetExceededError(Exception):
    """An enforcing budget is over its limit; a new run must be blocked."""

    def __init__(self, scope: str, period: str) -> None:
        self.scope = scope
        self.period = period
        super().__init__(
            f"The {scope} {period} usage budget has been exceeded. "
            "Contact an administrator or wait for the next period."
        )


class BudgetService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_for_scope(
        self, user_id: UUID, scope: BudgetScope, scope_id: UUID
    ) -> list[tuple[UsageBudget, Decimal, int, str]]:
        await self._assert_read_access(scope, scope_id, user_id)
        result = await self.session.execute(
            select(UsageBudget)
            .where(UsageBudget.scope == scope, UsageBudget.scope_id == scope_id)
            .order_by(UsageBudget.period)
        )
        budgets = list(result.scalars().all())
        enriched: list[tuple[UsageBudget, Decimal, int, str]] = []
        for budget in budgets:
            cost, tokens = await self._current_usage(budget.scope, budget.scope_id, budget.period)
            enriched.append((budget, cost, tokens, self._status(budget, cost, tokens)))
        return enriched

    async def status_for(self, budget: UsageBudget) -> tuple[Decimal, int, str]:
        cost, tokens = await self._current_usage(budget.scope, budget.scope_id, budget.period)
        return cost, tokens, self._status(budget, cost, tokens)

    # ------------------------------------------------------------------
    # Mutations (role-gated)
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        user_id: UUID,
        scope: BudgetScope,
        scope_id: UUID,
        period: str,
        limit_cost: float | None,
        limit_tokens: int | None,
        threshold_percent: int,
        enforce: bool,
    ) -> UsageBudget:
        await self._assert_manage_access(scope, scope_id, user_id)
        existing = await self.session.execute(
            select(UsageBudget.id).where(
                UsageBudget.scope == scope,
                UsageBudget.scope_id == scope_id,
                UsageBudget.period == period,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise BudgetConflictError
        budget = UsageBudget(
            scope=scope,
            scope_id=scope_id,
            period=period,
            limit_cost=Decimal(str(limit_cost)) if limit_cost is not None else None,
            limit_tokens=limit_tokens,
            threshold_percent=threshold_percent,
            enforce=enforce,
            created_by=user_id,
        )
        self.session.add(budget)
        await self.session.commit()
        await self.session.refresh(budget)
        await self._audit(user_id, budget, "budget_create")
        return budget

    async def update(
        self,
        *,
        user_id: UUID,
        budget_id: UUID,
        changes: dict,
    ) -> UsageBudget:
        budget = await self.session.get(UsageBudget, budget_id)
        if budget is None:
            raise BudgetNotFoundError
        await self._assert_manage_access(budget.scope, budget.scope_id, user_id)
        for key, value in changes.items():
            if key == "limit_cost" and value is not None:
                value = Decimal(str(value))
            setattr(budget, key, value)
        if budget.limit_cost is None and budget.limit_tokens is None:
            raise ValueError("At least one of limit_cost or limit_tokens is required")
        self.session.add(budget)
        await self.session.commit()
        await self.session.refresh(budget)
        await self._audit(user_id, budget, "budget_update")
        return budget

    async def delete(self, *, user_id: UUID, budget_id: UUID) -> None:
        budget = await self.session.get(UsageBudget, budget_id)
        if budget is None:
            raise BudgetNotFoundError
        await self._assert_manage_access(budget.scope, budget.scope_id, user_id)
        # Snapshot audit fields before the row is removed.
        workspace_id = budget.scope_id if budget.scope == "workspace" else None
        metadata = self._audit_metadata(budget)
        await self.session.delete(budget)
        await self.session.commit()
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace_id,
            action="budget_delete",
            target_type="usage_budget",
            target_id=budget_id,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Enforcement (called before starting a run)
    # ------------------------------------------------------------------

    async def assert_within_budget(self, workspace: Workspace, user_id: UUID) -> None:
        """Block the run if an enforcing budget is over limit; warn at threshold."""
        budgets = await self._applicable_budgets(workspace)
        for budget in budgets:
            cost, tokens = await self._current_usage(budget.scope, budget.scope_id, budget.period)
            status = self._status(budget, cost, tokens)
            if status == "over" and budget.enforce:
                raise BudgetExceededError(budget.scope, budget.period)
            if status in ("warning", "over"):
                await self._notify_threshold(workspace, user_id, budget, status)

    async def _applicable_budgets(self, workspace: Workspace) -> list[UsageBudget]:
        conditions = [
            and_(UsageBudget.scope == "workspace", UsageBudget.scope_id == workspace.id)
        ]
        if workspace.team_id is not None:
            conditions.append(
                and_(UsageBudget.scope == "team", UsageBudget.scope_id == workspace.team_id)
            )
            team = await self.session.get(Team, workspace.team_id)
            if team is not None:
                conditions.append(
                    and_(
                        UsageBudget.scope == "project",
                        UsageBudget.scope_id == team.project_id,
                    )
                )
        result = await self.session.execute(select(UsageBudget).where(or_(*conditions)))
        return list(result.scalars().all())

    async def _notify_threshold(
        self, workspace: Workspace, user_id: UUID, budget: UsageBudget, status: str
    ) -> None:
        verb = "exceeded" if status == "over" else "near"
        await NotificationService(self.session).create(
            user_id=user_id,
            workspace_id=workspace.id,
            notification_type="budget_warning",
            title=f"Usage budget {verb}",
            message=(
                f"The {budget.scope} {budget.period} usage budget is {status} "
                f"(threshold {budget.threshold_percent}%)."
            ),
        )

    # ------------------------------------------------------------------
    # Usage + status helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _period_start(period: str, now: datetime) -> datetime | None:
        if period == "total":
            return None
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "daily":
            return start_of_day
        if period == "weekly":
            return start_of_day - timedelta(days=start_of_day.weekday())
        if period == "monthly":
            return start_of_day.replace(day=1)
        return None

    async def _current_usage(
        self, scope: str, scope_id: UUID, period: str
    ) -> tuple[Decimal, int]:
        start = self._period_start(period, datetime.now(UTC))
        stmt = select(
            func.coalesce(func.sum(AgentRun.estimated_cost), 0),
            func.coalesce(func.sum(AgentRun.tokens_used), 0),
        )
        if scope == "workspace":
            stmt = stmt.where(AgentRun.workspace_id == scope_id)
        elif scope == "team":
            stmt = stmt.join(Workspace, Workspace.id == AgentRun.workspace_id).where(
                Workspace.team_id == scope_id
            )
        elif scope == "project":
            stmt = (
                stmt.join(Workspace, Workspace.id == AgentRun.workspace_id)
                .join(Team, Team.id == Workspace.team_id)
                .where(Team.project_id == scope_id)
            )
        if start is not None:
            stmt = stmt.where(AgentRun.started_at >= start)
        row = (await self.session.execute(stmt)).one()
        return Decimal(row[0]), int(row[1])

    @staticmethod
    def _status(budget: UsageBudget, cost: Decimal, tokens: int) -> str:
        ratios: list[Decimal] = []
        if budget.limit_cost is not None and budget.limit_cost > 0:
            ratios.append(cost / budget.limit_cost)
        if budget.limit_tokens is not None and budget.limit_tokens > 0:
            ratios.append(Decimal(tokens) / Decimal(budget.limit_tokens))
        if not ratios:
            return "ok"
        peak = max(ratios)
        if peak >= 1:
            return "over"
        if peak * 100 >= budget.threshold_percent:
            return "warning"
        return "ok"

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------

    async def _assert_read_access(self, scope: str, scope_id: UUID, user_id: UUID) -> None:
        if scope == "workspace":
            workspace = await self.session.get(Workspace, scope_id)
            if workspace is None:
                raise BudgetAccessError("No access to this workspace")
            if workspace.owner_id == user_id:
                return
            if workspace.team_id is not None and await self._is_project_member_of_team(
                workspace.team_id, user_id
            ):
                return
            raise BudgetAccessError("No access to this workspace")
        if scope == "team":
            team = await self.session.get(Team, scope_id)
            if team is None or not await self._is_project_member(team.project_id, user_id):
                raise BudgetAccessError("No access to this team")
            return
        if scope == "project":
            if not await self._is_project_member(scope_id, user_id):
                raise BudgetAccessError("No access to this project")
            return
        raise BudgetAccessError(f"Unknown scope: {scope}")

    async def _assert_manage_access(self, scope: str, scope_id: UUID, user_id: UUID) -> None:
        if scope == "workspace":
            workspace = await self.session.get(Workspace, scope_id)
            if workspace is None or workspace.owner_id != user_id:
                raise BudgetAccessError("Only the workspace owner can manage this budget")
            return
        if scope == "team":
            team = await self.session.get(Team, scope_id)
            if team is None:
                raise BudgetAccessError("No access to this team")
            if await self._is_team_lead(scope_id, user_id) or await self._is_project_admin(
                team.project_id, user_id
            ):
                return
            raise BudgetAccessError("Only a team lead or project admin can manage this budget")
        if scope == "project":
            if await self._is_project_admin(scope_id, user_id):
                return
            raise BudgetAccessError("Only a project admin can manage this budget")
        raise BudgetAccessError(f"Unknown scope: {scope}")

    async def _is_project_member(self, project_id: UUID, user_id: UUID) -> bool:
        result = await self.session.execute(
            select(ProjectMember.id).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def _is_project_member_of_team(self, team_id: UUID, user_id: UUID) -> bool:
        team = await self.session.get(Team, team_id)
        if team is None:
            return False
        return await self._is_project_member(team.project_id, user_id)

    async def _is_project_admin(self, project_id: UUID, user_id: UUID) -> bool:
        result = await self.session.execute(
            select(ProjectMember.id).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
                ProjectMember.role == "admin",
            )
        )
        return result.scalar_one_or_none() is not None

    async def _is_team_lead(self, team_id: UUID, user_id: UUID) -> bool:
        result = await self.session.execute(
            select(TeamMember.id).where(
                TeamMember.team_id == team_id,
                TeamMember.user_id == user_id,
                TeamMember.role == "lead",
            )
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    def _audit_metadata(budget: UsageBudget) -> dict:
        return {
            "scope": budget.scope,
            "scope_id": str(budget.scope_id),
            "period": budget.period,
            "limit_cost": float(budget.limit_cost) if budget.limit_cost is not None else None,
            "limit_tokens": budget.limit_tokens,
            "enforce": budget.enforce,
        }

    async def _audit(self, user_id: UUID, budget: UsageBudget, action: str) -> None:
        workspace_id = budget.scope_id if budget.scope == "workspace" else None
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace_id,
            action=action,
            target_type="usage_budget",
            target_id=budget.id,
            metadata=self._audit_metadata(budget),
        )
