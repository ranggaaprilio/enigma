"""Spec-Driven Development (SDD) orchestration.

Drives the GitHub Spec Kit pipeline
(constitution -> specify -> clarify -> plan -> analyze -> tasks -> implement) on top of the
existing agent infrastructure. Each phase is one agent run: ``SpecKitService`` assembles the
prompt from the Spec Kit command file + template + prior artifacts, runs the agent against the
sandbox (in a per-feature git worktree), then halts at ``awaiting_review`` until a human
approves. The ``implement`` phase is hard-blocked until ``plan`` and ``tasks`` are approved.

Scaffolding and git (``specify init``, ``git worktree``, commits) run directly through
``DaytonaRuntime.exec`` because the agent tool sandbox blocks ``git``/``uv``/scripts. Artifact
*content* is produced by the agent (which can write into ``specs/`` and ``.specify/``).
"""

import asyncio
import re
import shlex
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.models import AgentRun, SpecPhase, SpecWorkflow, User, Workspace
from app.services.agents import AgentService
from app.services.audit import AuditLogService
from app.services.daytona_runtime import DaytonaRuntime, get_daytona_runtime
from app.services.notifications import NotificationService
from app.services.repositories import NoChangesToCommitError, RepositoryService
from app.services.workspaces import WorkspaceNotFoundError, WorkspaceSecretCipher

# Phases, in pipeline order.
PHASE_ORDER = (
    "constitution",
    "specify",
    "clarify",
    "plan",
    "analyze",
    "tasks",
    "implement",
)
# Phases that halt for human review once their agent run completes.
REVIEW_PHASES = frozenset({"constitution", "specify", "clarify", "plan", "tasks"})
# Phases that simply complete (advisory / terminal).
TERMINAL_PHASES = frozenset({"analyze", "implement"})
# A phase may run only once these phases are approved. ``implement`` is the hard gate.
PREREQUISITES: dict[str, tuple[str, ...]] = {
    "constitution": (),
    "specify": (),
    "clarify": ("specify",),
    "plan": ("specify",),
    "analyze": ("plan",),
    "tasks": ("plan",),
    "implement": ("plan", "tasks"),
}

# `specify init` flags. Build-time network is allowed; this runs offline from bundled
# templates. NOTE: verify these flags against the installed specify-cli version.
SPECIFY_INIT_COMMAND = "specify init . --ai claude --script sh --ignore-agent-tools --force"

GIT_TIMEOUT_SECONDS = 120
# Upper bound for how long a phase's agent run may take before the finalizer gives up.
PHASE_FINALIZE_TIMEOUT_SECONDS = 1800
PHASE_POLL_INTERVAL_SECONDS = 2.0

_TERMINAL_RUN_STATUSES = ("completed", "error", "stopped")


class SpecKitError(Exception):
    pass


class SpecWorkflowNotFoundError(SpecKitError):
    pass


class SpecPhaseNotFoundError(SpecKitError):
    pass


class InvalidSpecPhaseError(SpecKitError):
    pass


class SpecPhaseNotReviewableError(SpecKitError):
    pass


class SpecGateError(SpecKitError):
    """A phase cannot run because its prerequisite phases are not approved."""


class SpecKitNotReadyError(SpecKitError):
    """The workspace is not ready for SDD (repo not cloned / init failed)."""


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:50] or "feature"


class PhasePlan:
    """Static description of how a phase maps to an artifact + Spec Kit assets."""

    def __init__(
        self,
        *,
        command: str,
        artifact: str | None,
        template: str | None,
        inputs: tuple[str, ...],
    ) -> None:
        self.command = command
        self.artifact = artifact
        self.template = template
        self.inputs = inputs


PHASE_PLANS: dict[str, PhasePlan] = {
    "constitution": PhasePlan(
        command="speckit.constitution",
        artifact=".specify/memory/constitution.md",
        template=None,
        inputs=(),
    ),
    "specify": PhasePlan(
        command="speckit.specify",
        artifact="{spec_dir}/spec.md",
        template="spec-template.md",
        inputs=(),
    ),
    "clarify": PhasePlan(
        command="speckit.clarify",
        artifact="{spec_dir}/spec.md",
        template=None,
        inputs=("spec.md",),
    ),
    "plan": PhasePlan(
        command="speckit.plan",
        artifact="{spec_dir}/plan.md",
        template="plan-template.md",
        inputs=("spec.md",),
    ),
    "analyze": PhasePlan(
        command="speckit.analyze",
        artifact="{spec_dir}/analysis.md",
        template=None,
        inputs=("spec.md", "plan.md", "tasks.md"),
    ),
    "tasks": PhasePlan(
        command="speckit.tasks",
        artifact="{spec_dir}/tasks.md",
        template="tasks-template.md",
        inputs=("spec.md", "plan.md"),
    ),
    "implement": PhasePlan(
        command="speckit.implement",
        artifact=None,
        template=None,
        inputs=("spec.md", "plan.md", "tasks.md"),
    ),
}


class SpecKitService:
    def __init__(
        self,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
        settings: Settings,
        runtime: DaytonaRuntime | None = None,
    ) -> None:
        self.session = session
        self.session_maker = session_maker
        self.settings = settings
        self.runtime = runtime or get_daytona_runtime()

    # ----- public API ---------------------------------------------------------------

    async def ensure_initialized(self, workspace_id: UUID, user_id: UUID) -> None:
        workspace = await self._get_workspace(workspace_id, user_id)
        if not await self._path_exists(workspace, workspace.workspace_path, ".git"):
            raise SpecKitNotReadyError("Repository is not cloned in the workspace yet")
        if await self._path_exists(workspace, workspace.workspace_path, ".specify"):
            return
        result = await self.runtime.exec(
            workspace,
            SPECIFY_INIT_COMMAND,
            cwd=workspace.workspace_path,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if result.exit_code != 0:
            raise SpecKitNotReadyError(
                f"specify init failed: {result.stderr or result.stdout}".strip()
            )
        # Commit the scaffold to the default branch so feature worktrees inherit it.
        repo_service = RepositoryService(
            self.session,
            WorkspaceSecretCipher(self.settings.workspace_env_encryption_key),
            self.runtime,
        )
        try:
            await repo_service.commit(workspace_id, user_id, "chore: initialize Spec Kit (SDD)")
        except NoChangesToCommitError:
            pass

    async def start_workflow(
        self,
        workspace_id: UUID,
        user_id: UUID,
        feature_name: str,
        description: str,
    ) -> SpecWorkflow:
        await self.ensure_initialized(workspace_id, user_id)

        count_result = await self.session.execute(
            select(func.count())
            .select_from(SpecWorkflow)
            .where(SpecWorkflow.workspace_id == workspace_id)
        )
        number = (count_result.scalar_one() or 0) + 1
        slug = f"{number:03d}-{_slugify(feature_name)}"

        workflow = SpecWorkflow(
            workspace_id=workspace_id,
            user_id=user_id,
            feature_name=feature_name,
            feature_slug=slug,
            description=description,
            branch=slug,
            spec_dir=f"specs/{slug}",
            worktree_path=None,
            status="pending",
            current_phase="specify",
        )
        self.session.add(workflow)
        await self.session.flush()
        for phase_name in PHASE_ORDER:
            self.session.add(SpecPhase(workflow_id=workflow.id, phase=phase_name, status="pending"))
        await self.session.commit()
        await self.session.refresh(workflow)

        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace_id,
            action="spec_workflow_start",
            target_type="spec_workflow",
            target_id=workflow.id,
            metadata={"feature_slug": slug, "feature_name": feature_name},
        )

        # Auto-run the first phase: starting a workflow generates spec.md.
        await self.run_phase(workspace_id, workflow.id, "specify", user_id)
        return await self.get_workflow(workspace_id, workflow.id, user_id)

    async def run_phase(
        self,
        workspace_id: UUID,
        workflow_id: UUID,
        phase_name: str,
        user_id: UUID,
        feedback: str | None = None,
    ) -> SpecPhase:
        if phase_name not in PHASE_PLANS:
            raise InvalidSpecPhaseError(phase_name)
        workspace = await self._get_workspace(workspace_id, user_id)
        workflow = await self._get_workflow_row(workspace_id, workflow_id)
        phase = await self._get_phase_row(workflow_id, phase_name)

        await self._assert_prerequisites(workflow_id, phase_name)

        if phase_name != "constitution":
            await self._ensure_worktree(workspace, workflow)
        working_dir = self._working_dir(workspace, workflow, phase_name)
        plan = PHASE_PLANS[phase_name]
        artifact_path = plan.artifact.format(spec_dir=workflow.spec_dir) if plan.artifact else None

        prompt = await self._build_prompt(workspace, workflow, phase_name, working_dir, feedback)

        agent_service = AgentService(
            session=self.session,
            session_maker=self.session_maker,
            settings=self.settings,
        )
        # Propagates AgentRunAlreadyActiveError (mapped to 409 by the router) when the
        # workspace already has an active run.
        run = await agent_service.create_run(
            workspace_id, user_id, prompt, working_dir=working_dir
        )

        phase.status = "running"
        phase.artifact_path = artifact_path
        phase.agent_run_id = run.id
        if feedback is not None:
            phase.attempt += 1
        workflow.status = "running"
        workflow.current_phase = phase_name
        self.session.add_all([phase, workflow])
        await self.session.commit()
        await self.session.refresh(phase)

        # Finalize off-request once the agent run reaches a terminal state.
        asyncio.create_task(self._finalize_phase(workflow_id, phase_name, run.id, user_id))
        return phase

    async def approve_phase(
        self,
        workspace_id: UUID,
        workflow_id: UUID,
        phase_name: str,
        user_id: UUID,
    ) -> SpecPhase:
        await self._get_workspace(workspace_id, user_id)
        await self._get_workflow_row(workspace_id, workflow_id)
        phase = await self._get_phase_row(workflow_id, phase_name)
        if phase.status != "awaiting_review":
            raise SpecPhaseNotReviewableError(phase_name)
        phase.status = "approved"
        phase.reviewed_by = user_id
        phase.reviewed_at = datetime.now(UTC)
        self.session.add(phase)
        await self.session.commit()
        await self.session.refresh(phase)

        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace_id,
            action="spec_phase_approved",
            target_type="spec_phase",
            target_id=phase.id,
            metadata={"phase": phase_name, "workflow_id": workflow_id, "attempt": phase.attempt},
        )
        await NotificationService(self.session).create(
            user_id=user_id,
            workspace_id=workspace_id,
            notification_type="spec_phase_approved",
            title="Spec phase approved",
            message=f"The '{phase_name}' phase was approved.",
        )
        return phase

    async def request_changes(
        self,
        workspace_id: UUID,
        workflow_id: UUID,
        phase_name: str,
        user_id: UUID,
        notes: str,
    ) -> SpecPhase:
        await self._get_workspace(workspace_id, user_id)
        await self._get_workflow_row(workspace_id, workflow_id)
        phase = await self._get_phase_row(workflow_id, phase_name)
        if phase.status != "awaiting_review":
            raise SpecPhaseNotReviewableError(phase_name)
        phase.status = "changes_requested"
        phase.review_notes = notes
        phase.reviewed_by = user_id
        phase.reviewed_at = datetime.now(UTC)
        self.session.add(phase)
        await self.session.commit()

        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace_id,
            action="spec_phase_changes_requested",
            target_type="spec_phase",
            target_id=phase.id,
            metadata={"phase": phase_name, "workflow_id": workflow_id},
        )
        await NotificationService(self.session).create(
            user_id=user_id,
            workspace_id=workspace_id,
            notification_type="spec_phase_changes_requested",
            title="Spec changes requested",
            message=f"Changes were requested on the '{phase_name}' phase; re-running.",
        )
        # Re-run the phase with the reviewer's feedback folded into the prompt.
        return await self.run_phase(
            workspace_id, workflow_id, phase_name, user_id, feedback=notes
        )

    async def list_workflows(self, workspace_id: UUID, user_id: UUID) -> list[SpecWorkflow]:
        await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(SpecWorkflow)
            .options(selectinload(SpecWorkflow.phases))
            .where(SpecWorkflow.workspace_id == workspace_id)
            .order_by(SpecWorkflow.created_at.desc())
        )
        workflows = list(result.scalars().all())
        for workflow in workflows:
            workflow.phases.sort(key=lambda p: PHASE_ORDER.index(p.phase))
        return workflows

    async def get_workflow(
        self, workspace_id: UUID, workflow_id: UUID, user_id: UUID
    ) -> SpecWorkflow:
        await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(SpecWorkflow)
            .options(selectinload(SpecWorkflow.phases))
            .where(
                SpecWorkflow.id == workflow_id,
                SpecWorkflow.workspace_id == workspace_id,
            )
        )
        workflow = result.scalar_one_or_none()
        if workflow is None:
            raise SpecWorkflowNotFoundError
        workflow.phases.sort(key=lambda p: PHASE_ORDER.index(p.phase))
        return workflow

    async def read_artifact(
        self,
        workspace_id: UUID,
        workflow_id: UUID,
        phase_name: str,
        user_id: UUID,
    ) -> tuple[str | None, str]:
        workspace = await self._get_workspace(workspace_id, user_id)
        workflow = await self._get_workflow_row(workspace_id, workflow_id)
        phase = await self._get_phase_row(workflow_id, phase_name)
        if not phase.artifact_path:
            return None, ""
        working_dir = self._working_dir(workspace, workflow, phase_name)
        content = await self._read_file(workspace, working_dir, phase.artifact_path)
        return phase.artifact_path, content or ""

    # ----- worktree + prompt helpers -----------------------------------------------

    def _working_dir(
        self, workspace: Workspace, workflow: SpecWorkflow, phase_name: str
    ) -> str:
        # Constitution is repo-level and lives in the base tree; every feature phase runs
        # in the feature worktree (falling back to the base tree if not created yet).
        if phase_name == "constitution":
            return workspace.workspace_path
        return workflow.worktree_path or workspace.workspace_path

    async def _ensure_worktree(self, workspace: Workspace, workflow: SpecWorkflow) -> None:
        if workflow.worktree_path:
            return
        slug = workflow.feature_slug
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            raise SpecKitError(f"Unsafe feature slug: {slug}")
        # Ensure the managed worktrees/ dir exists (sibling to the base clone).
        await self.runtime.exec(
            workspace, "mkdir -p ../worktrees", cwd=workspace.workspace_path
        )
        result = await self._run_git(
            workspace,
            ["worktree", "add", f"../worktrees/{slug}", "-b", workflow.branch,
             workspace.default_branch],
            cwd=workspace.workspace_path,
        )
        if result.exit_code != 0:
            raise SpecKitError(
                f"git worktree add failed: {result.stderr or result.stdout}".strip()
            )
        workflow.worktree_path = f"worktrees/{slug}"
        self.session.add(workflow)
        await self.session.commit()
        await self.session.refresh(workflow)

    async def _build_prompt(
        self,
        workspace: Workspace,
        workflow: SpecWorkflow,
        phase_name: str,
        working_dir: str,
        feedback: str | None,
    ) -> str:
        plan = PHASE_PLANS[phase_name]
        artifact_path = plan.artifact.format(spec_dir=workflow.spec_dir) if plan.artifact else None

        command_doc = await self._read_file(
            workspace, working_dir, f".claude/commands/{plan.command}.md"
        )
        template_doc = None
        if plan.template:
            template_doc = await self._read_file(
                workspace, working_dir, f".specify/templates/{plan.template}"
            )

        sections: list[str] = [
            f"You are executing the '{phase_name}' phase of a Spec-Driven Development "
            "workflow based on the GitHub Spec Kit. Produce the artifact for this phase only.",
            f"Feature: {workflow.feature_name}\n\nDescription:\n{workflow.description}",
        ]
        if command_doc:
            sections.append(
                "Spec Kit guidance for this phase (adapt it for non-interactive execution; "
                "do NOT run scripts or git):\n\n" + command_doc.strip()
            )
        if template_doc:
            sections.append("Use this template structure:\n\n" + template_doc.strip())

        for input_name in plan.inputs:
            prior = await self._read_file(
                workspace, working_dir, f"{workflow.spec_dir}/{input_name}"
            )
            if prior:
                sections.append(f"Existing {input_name}:\n\n{prior.strip()}")

        if feedback:
            sections.append(
                "A reviewer requested changes. Address this feedback and regenerate the "
                f"artifact:\n\n{feedback.strip()}"
            )

        if artifact_path:
            sections.append(
                "When finished, write the COMPLETE artifact to the file "
                f"`{artifact_path}` using the write_file tool. Do not run git or shell "
                "scripts; only read and write files. Output only the artifact content to the file."
            )
        else:
            sections.append(
                "Implement the tasks by editing files in this worktree using the available "
                "tools. Do not run git; the platform will commit your changes."
            )
        return "\n\n---\n\n".join(sections)

    # ----- finalizer ----------------------------------------------------------------

    async def _finalize_phase(
        self,
        workflow_id: UUID,
        phase_name: str,
        run_id: UUID,
        user_id: UUID,
    ) -> None:
        run_status = await self._await_run_terminal(run_id)
        async with self.session_maker() as session:
            workflow = await session.get(SpecWorkflow, workflow_id)
            phase = await self._load_phase(session, workflow_id, phase_name)
            if workflow is None or phase is None:
                return
            workspace = await session.get(Workspace, workflow.workspace_id)
            if workspace is None:
                return

            if run_status != "completed":
                phase.status = "error"
                workflow.status = "error"
                session.add_all([phase, workflow])
                await session.commit()
                await self._notify_and_audit(
                    session,
                    workspace_id=workflow.workspace_id,
                    user_id=user_id,
                    action="spec_phase_error",
                    phase=phase,
                    notification_type="spec_phase_error",
                    title="Spec phase failed",
                    message=f"The '{phase_name}' phase ended with status {run_status}.",
                )
                return

            await self._commit_phase_artifact(session, workspace, workflow, phase_name, user_id)

            if phase_name in REVIEW_PHASES:
                phase.status = "awaiting_review"
                workflow.status = "awaiting_review"
            else:
                phase.status = "completed"
                workflow.status = "completed" if phase_name == "implement" else "running"
            session.add_all([phase, workflow])
            await session.commit()

            if phase.status == "awaiting_review":
                await self._notify_and_audit(
                    session,
                    workspace_id=workflow.workspace_id,
                    user_id=user_id,
                    action="spec_phase_awaiting_review",
                    phase=phase,
                    notification_type="spec_phase_awaiting_review",
                    title="Spec phase needs review",
                    message=f"The '{phase_name}' phase is ready for review.",
                )
            else:
                await self._notify_and_audit(
                    session,
                    workspace_id=workflow.workspace_id,
                    user_id=user_id,
                    action="spec_phase_completed",
                    phase=phase,
                    notification_type="spec_phase_completed",
                    title="Spec phase completed",
                    message=f"The '{phase_name}' phase completed.",
                )

    async def _await_run_terminal(self, run_id: UUID) -> str:
        elapsed = 0.0
        while elapsed < PHASE_FINALIZE_TIMEOUT_SECONDS:
            async with self.session_maker() as session:
                run = await session.get(AgentRun, run_id)
                if run is None:
                    return "error"
                if run.status in _TERMINAL_RUN_STATUSES:
                    return run.status
            await asyncio.sleep(PHASE_POLL_INTERVAL_SECONDS)
            elapsed += PHASE_POLL_INTERVAL_SECONDS
        return "error"

    async def _commit_phase_artifact(
        self,
        session: AsyncSession,
        workspace: Workspace,
        workflow: SpecWorkflow,
        phase_name: str,
        user_id: UUID,
    ) -> None:
        working_dir = self._working_dir(workspace, workflow, phase_name)
        user = await session.get(User, user_id)
        name = (user.name if user and user.name else (user.email if user else "Enigma")) or "Enigma"
        email = user.email if user else "enigma@localhost"
        message = f"spec({workflow.feature_slug}): {phase_name}"

        await self.runtime.exec(workspace, "git add -A", cwd=working_dir,
                                timeout=GIT_TIMEOUT_SECONDS)
        commit = await self._run_git(
            workspace,
            ["-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", message],
            cwd=working_dir,
        )
        # A no-op commit (nothing changed) is not an error for our purposes.
        if commit.exit_code != 0 and "nothing to commit" not in (
            commit.stdout + commit.stderr
        ).lower():
            raise SpecKitError(
                f"git commit failed: {commit.stderr or commit.stdout}".strip()
            )

    # ----- low-level helpers --------------------------------------------------------

    async def _run_git(self, workspace: Workspace, args: list[str], *, cwd: str):
        command = "git " + " ".join(shlex.quote(arg) for arg in args)
        return await self.runtime.exec(
            workspace, command, cwd=cwd, timeout=GIT_TIMEOUT_SECONDS
        )

    async def _read_file(self, workspace: Workspace, cwd: str, rel_path: str) -> str | None:
        result = await self.runtime.exec(
            workspace,
            f"cat {shlex.quote(rel_path)}",
            cwd=cwd,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if result.exit_code != 0:
            return None
        return result.stdout

    async def _path_exists(self, workspace: Workspace, cwd: str, rel_path: str) -> bool:
        result = await self.runtime.exec(
            workspace,
            f"test -e {shlex.quote(rel_path)}",
            cwd=cwd,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        return result.exit_code == 0

    async def _assert_prerequisites(self, workflow_id: UUID, phase_name: str) -> None:
        required = PREREQUISITES.get(phase_name, ())
        if not required:
            return
        result = await self.session.execute(
            select(SpecPhase.phase, SpecPhase.status).where(
                SpecPhase.workflow_id == workflow_id,
                SpecPhase.phase.in_(required),
            )
        )
        statuses = {phase: status for phase, status in result.all()}
        missing = [name for name in required if statuses.get(name) != "approved"]
        if missing:
            raise SpecGateError(
                f"Phase '{phase_name}' requires approved: {', '.join(missing)}"
            )

    async def _get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        return workspace

    async def _get_workflow_row(self, workspace_id: UUID, workflow_id: UUID) -> SpecWorkflow:
        workflow = await self.session.get(SpecWorkflow, workflow_id)
        if workflow is None or workflow.workspace_id != workspace_id:
            raise SpecWorkflowNotFoundError
        return workflow

    async def _get_phase_row(self, workflow_id: UUID, phase_name: str) -> SpecPhase:
        if phase_name not in PHASE_PLANS:
            raise InvalidSpecPhaseError(phase_name)
        phase = await self._load_phase(self.session, workflow_id, phase_name)
        if phase is None:
            raise SpecPhaseNotFoundError(phase_name)
        return phase

    @staticmethod
    async def _load_phase(
        session: AsyncSession, workflow_id: UUID, phase_name: str
    ) -> SpecPhase | None:
        result = await session.execute(
            select(SpecPhase).where(
                SpecPhase.workflow_id == workflow_id,
                SpecPhase.phase == phase_name,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _notify_and_audit(
        session: AsyncSession,
        *,
        workspace_id: UUID,
        user_id: UUID,
        action: str,
        phase: SpecPhase,
        notification_type: str,
        title: str,
        message: str,
    ) -> None:
        await AuditLogService(session).record(
            user_id=user_id,
            workspace_id=workspace_id,
            action=action,
            target_type="spec_phase",
            target_id=phase.id,
            metadata={"phase": phase.phase, "workflow_id": phase.workflow_id},
        )
        await NotificationService(session).create(
            user_id=user_id,
            workspace_id=workspace_id,
            notification_type=notification_type,
            title=title,
            message=message,
        )
