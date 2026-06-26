from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectMember, Team, TeamMember
from app.services.utils import slugify


class ProjectNotFoundError(Exception):
    pass


class ProjectPermissionError(Exception):
    pass


class ProjectMemberNotFoundError(Exception):
    pass


class TeamNotFoundError(Exception):
    pass


class TeamPermissionError(Exception):
    pass


class TeamMemberNotFoundError(Exception):
    pass


class SqlAlchemyProjectService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── helpers ──────────────────────────────────────────────────────────────

    async def _require_project_member(self, project_id: UUID, user_id: UUID) -> ProjectMember:
        result = await self.session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise ProjectNotFoundError
        return member

    async def _require_project_admin(self, project_id: UUID, user_id: UUID) -> ProjectMember:
        member = await self._require_project_member(project_id, user_id)
        if member.role != "admin":
            raise ProjectPermissionError
        return member

    async def _get_project(self, project_id: UUID) -> Project:
        project = await self.session.get(Project, project_id)
        if project is None:
            raise ProjectNotFoundError
        return project

    # ── project CRUD ─────────────────────────────────────────────────────────

    async def list_accessible(self, user_id: UUID) -> list[Project]:
        result = await self.session.execute(
            select(Project)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(ProjectMember.user_id == user_id)
            .order_by(Project.created_at)
        )
        return list(result.scalars().all())

    async def get_accessible(self, project_id: UUID, user_id: UUID) -> Project:
        await self._require_project_member(project_id, user_id)
        return await self._get_project(project_id)

    async def create(
        self,
        name: str,
        created_by_id: UUID,
        description: str | None = None,
    ) -> Project:
        project = Project(
            name=name,
            slug=slugify(name, fallback="project"),
            description=description,
            created_by=created_by_id,
        )
        self.session.add(project)
        await self.session.flush()

        member = ProjectMember(
            project_id=project.id,
            user_id=created_by_id,
            role="admin",
        )
        self.session.add(member)
        await self.session.commit()
        await self.session.refresh(project)
        return project

    async def update(
        self,
        project_id: UUID,
        user_id: UUID,
        name: str | None = None,
        description: str | None = None,
    ) -> Project:
        await self._require_project_admin(project_id, user_id)
        project = await self._get_project(project_id)
        if name is not None:
            project.name = name
            project.slug = slugify(name, fallback="project")
        if description is not None:
            project.description = description
        self.session.add(project)
        await self.session.commit()
        await self.session.refresh(project)
        return project

    async def delete(self, project_id: UUID, user_id: UUID) -> None:
        await self._require_project_admin(project_id, user_id)
        project = await self._get_project(project_id)
        await self.session.delete(project)
        await self.session.commit()

    # ── project members ───────────────────────────────────────────────────────

    async def list_members(self, project_id: UUID, user_id: UUID) -> list[ProjectMember]:
        await self._require_project_member(project_id, user_id)
        result = await self.session.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.joined_at)
        )
        return list(result.scalars().all())

    async def add_member(
        self,
        project_id: UUID,
        actor_id: UUID,
        target_user_id: UUID,
        role: str,
    ) -> ProjectMember:
        await self._require_project_admin(project_id, actor_id)
        result = await self.session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == target_user_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            existing.role = role
            self.session.add(existing)
            await self.session.commit()
            await self.session.refresh(existing)
            return existing

        member = ProjectMember(
            project_id=project_id,
            user_id=target_user_id,
            role=role,
        )
        self.session.add(member)
        await self.session.commit()
        await self.session.refresh(member)
        return member

    async def update_member_role(
        self,
        project_id: UUID,
        actor_id: UUID,
        target_user_id: UUID,
        role: str,
    ) -> ProjectMember:
        await self._require_project_admin(project_id, actor_id)
        result = await self.session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == target_user_id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise ProjectMemberNotFoundError
        member.role = role
        self.session.add(member)
        await self.session.commit()
        await self.session.refresh(member)
        return member

    async def remove_member(
        self,
        project_id: UUID,
        actor_id: UUID,
        target_user_id: UUID,
    ) -> None:
        await self._require_project_admin(project_id, actor_id)
        result = await self.session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == target_user_id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise ProjectMemberNotFoundError
        await self.session.delete(member)
        await self.session.commit()

    # ── teams ─────────────────────────────────────────────────────────────────

    async def _require_team_access(self, team_id: UUID, user_id: UUID) -> Team:
        """Return team if user is a project member of the team's project."""
        team = await self.session.get(Team, team_id)
        if team is None:
            raise TeamNotFoundError
        await self._require_project_member(team.project_id, user_id)
        return team

    async def _require_team_write(self, team_id: UUID, user_id: UUID) -> Team:
        """Allow write if project admin OR team lead."""
        team = await self.session.get(Team, team_id)
        if team is None:
            raise TeamNotFoundError

        project_member_result = await self.session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == team.project_id,
                ProjectMember.user_id == user_id,
            )
        )
        pm = project_member_result.scalar_one_or_none()
        if pm is None:
            raise TeamNotFoundError

        if pm.role == "admin":
            return team

        tm_result = await self.session.execute(
            select(TeamMember).where(
                TeamMember.team_id == team_id,
                TeamMember.user_id == user_id,
            )
        )
        tm = tm_result.scalar_one_or_none()
        if tm is None or tm.role != "lead":
            raise TeamPermissionError
        return team

    async def list_teams(self, project_id: UUID, user_id: UUID) -> list[Team]:
        await self._require_project_member(project_id, user_id)
        result = await self.session.execute(
            select(Team).where(Team.project_id == project_id).order_by(Team.created_at)
        )
        return list(result.scalars().all())

    async def get_team(self, team_id: UUID, user_id: UUID) -> Team:
        return await self._require_team_access(team_id, user_id)

    async def create_team(
        self,
        project_id: UUID,
        name: str,
        created_by_id: UUID,
        description: str | None = None,
    ) -> Team:
        pm = await self._require_project_member(project_id, created_by_id)
        if pm.role == "viewer":
            raise TeamPermissionError

        team = Team(
            project_id=project_id,
            name=name,
            slug=slugify(name, fallback="team"),
            description=description,
            created_by=created_by_id,
        )
        self.session.add(team)
        await self.session.flush()

        member = TeamMember(
            team_id=team.id,
            user_id=created_by_id,
            role="lead",
        )
        self.session.add(member)
        await self.session.commit()
        await self.session.refresh(team)
        return team

    async def update_team(
        self,
        team_id: UUID,
        user_id: UUID,
        name: str | None = None,
        description: str | None = None,
    ) -> Team:
        team = await self._require_team_write(team_id, user_id)
        if name is not None:
            team.name = name
            team.slug = slugify(name, fallback="team")
        if description is not None:
            team.description = description
        self.session.add(team)
        await self.session.commit()
        await self.session.refresh(team)
        return team

    async def delete_team(self, team_id: UUID, user_id: UUID) -> None:
        team = await self.session.get(Team, team_id)
        if team is None:
            raise TeamNotFoundError
        await self._require_project_admin(team.project_id, user_id)
        await self.session.delete(team)
        await self.session.commit()

    # ── team members ─────────────────────────────────────────────────────────

    async def list_team_members(self, team_id: UUID, user_id: UUID) -> list[TeamMember]:
        await self._require_team_access(team_id, user_id)
        result = await self.session.execute(
            select(TeamMember).where(TeamMember.team_id == team_id).order_by(TeamMember.joined_at)
        )
        return list(result.scalars().all())

    async def add_team_member(
        self,
        team_id: UUID,
        actor_id: UUID,
        target_user_id: UUID,
        role: str,
    ) -> TeamMember:
        await self._require_team_write(team_id, actor_id)
        result = await self.session.execute(
            select(TeamMember).where(
                TeamMember.team_id == team_id,
                TeamMember.user_id == target_user_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            existing.role = role
            self.session.add(existing)
            await self.session.commit()
            await self.session.refresh(existing)
            return existing

        member = TeamMember(team_id=team_id, user_id=target_user_id, role=role)
        self.session.add(member)
        await self.session.commit()
        await self.session.refresh(member)
        return member

    async def update_team_member_role(
        self,
        team_id: UUID,
        actor_id: UUID,
        target_user_id: UUID,
        role: str,
    ) -> TeamMember:
        await self._require_team_write(team_id, actor_id)
        result = await self.session.execute(
            select(TeamMember).where(
                TeamMember.team_id == team_id,
                TeamMember.user_id == target_user_id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise TeamMemberNotFoundError
        member.role = role
        self.session.add(member)
        await self.session.commit()
        await self.session.refresh(member)
        return member

    async def remove_team_member(
        self,
        team_id: UUID,
        actor_id: UUID,
        target_user_id: UUID,
    ) -> None:
        await self._require_team_write(team_id, actor_id)
        result = await self.session.execute(
            select(TeamMember).where(
                TeamMember.team_id == team_id,
                TeamMember.user_id == target_user_id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise TeamMemberNotFoundError
        await self.session.delete(member)
        await self.session.commit()
