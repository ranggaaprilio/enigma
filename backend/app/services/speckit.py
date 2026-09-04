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
import json
import re
import shlex
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.models import AgentRun, SpecPhase, SpecWorkflow, User, Workspace
from app.services.agents import AgentService
from app.services.audit import AuditLogService
from app.services.daytona_runtime import DaytonaRuntime, get_daytona_runtime
from app.services.notifications import NotificationService
from app.services.repositories import (
    DEFAULT_COMMIT_AUTHOR,
    DEFAULT_COMMIT_EMAIL,
    NoChangesToCommitError,
    RepositoryService,
)
from app.services.workspaces import WorkspaceNotFoundError, WorkspaceSecretCipher

logger = structlog.get_logger(__name__)

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
# The agent-selection flag was renamed `--ai` -> `--integration` in recent spec-kit
# releases; the Daytona snapshot tracks latest (see daytona/Dockerfile SPEC_KIT_REF).
SPECIFY_INIT_COMMAND = (
    "specify init . --integration claude --script sh --ignore-agent-tools --force"
)

GIT_TIMEOUT_SECONDS = 120
# Upper bound for how long a phase's agent run may take before the finalizer gives up.
PHASE_FINALIZE_TIMEOUT_SECONDS = 1800
PHASE_POLL_INTERVAL_SECONDS = 2.0

# Spec Kit's constitution template ships with bracketed UPPER_SNAKE placeholder tokens
# (e.g. [PROJECT_NAME], [PRINCIPLE_1_NAME], [RATIFICATION_DATE]). Their presence means
# `/speckit.constitution` has not filled the file in yet, so the constitution is unratified.
# The required underscore avoids false positives on ordinary bracketed acronyms (e.g. [API]).
CONSTITUTION_PLACEHOLDER = re.compile(r"\[[A-Z][A-Z0-9]*_[A-Z0-9_]*\]")

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


class InvalidArtifactPathError(SpecKitError):
    """A requested artifact path escapes the workflow's spec directory."""


class SpecGateError(SpecKitError):
    """A phase cannot run because its prerequisite phases are not approved."""


class SpecFeatureSlugConflictError(SpecKitError):
    """Another workflow already owns this feature slug (branch + ``specs/`` directory)."""


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
        design_artifacts: tuple[str, ...] = (),
    ) -> None:
        self.command = command
        self.artifact = artifact
        self.template = template
        self.inputs = inputs
        # Supporting files produced alongside ``artifact`` (relative to ``spec_dir``). A trailing
        # slash marks a directory whose files are enumerated rather than a single file.
        self.design_artifacts = design_artifacts


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
        design_artifacts=("research.md", "data-model.md", "contracts/", "quickstart.md"),
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
        # Strong references to in-flight phase finalizers, so the event loop cannot
        # garbage-collect a running task out from under a workflow.
        self._finalizers: set[asyncio.Task] = set()

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

        # Constitution-first: only skip straight to ``specify`` when the repo already has a
        # ratified constitution; otherwise generate it first (auto-chained to specify below).
        workspace = await self._get_workspace(workspace_id, user_id)
        first_phase = "specify" if await self._constitution_ratified(workspace) else "constitution"

        # Number from the highest slug already used, not from a row count: a deleted
        # workflow would otherwise hand its number to the next feature, colliding on both
        # the branch and the `specs/<slug>` directory. Concurrent starts are caught by the
        # unique constraint on (workspace_id, feature_slug) below.
        number = await self._next_feature_number(workspace_id)
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
            current_phase=first_phase,
        )
        self.session.add(workflow)
        try:
            await self.session.flush()
            for phase_name in PHASE_ORDER:
                self.session.add(
                    SpecPhase(workflow_id=workflow.id, phase=phase_name, status="pending")
                )
            await self.session.commit()
        except IntegrityError as error:
            # Two concurrent starts raced for the same slug; the loser retries with a name.
            await self.session.rollback()
            raise SpecFeatureSlugConflictError(
                f"A workflow for '{slug}' already exists in this workspace"
            ) from error
        await self.session.refresh(workflow)

        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace_id,
            action="spec_workflow_start",
            target_type="spec_workflow",
            target_id=workflow.id,
            metadata={"feature_slug": slug, "feature_name": feature_name},
        )

        # Auto-run the first phase. When there is no ratified constitution yet this generates it
        # first; ``_finalize_phase`` then auto-chains ``specify`` once the constitution completes.
        await self.run_phase(workspace_id, workflow.id, first_phase, user_id)
        return await self.get_workflow(workspace_id, workflow.id, user_id)

    async def _next_feature_number(self, workspace_id: UUID) -> int:
        """One past the highest ``NNN-`` prefix already used in this workspace."""
        result = await self.session.execute(
            select(SpecWorkflow.feature_slug).where(
                SpecWorkflow.workspace_id == workspace_id
            )
        )
        highest = 0
        for slug in result.scalars().all():
            prefix = slug.split("-", 1)[0]
            if prefix.isdigit():
                highest = max(highest, int(prefix))
        return highest + 1

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

        # Finalize off-request once the agent run reaches a terminal state. The task is
        # retained until it finishes; a bare `create_task` reference can be collected
        # mid-flight, silently abandoning the phase in `running`.
        task = asyncio.create_task(
            self._finalize_phase(workflow_id, phase_name, run.id, user_id)
        )
        self._finalizers.add(task)
        task.add_done_callback(self._finalizers.discard)
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

    async def get_open_questions(
        self,
        workspace_id: UUID,
        workflow_id: UUID,
        user_id: UUID,
    ) -> list[dict]:
        """Read the structured open questions produced by the latest ``analyze`` run."""
        workspace = await self._get_workspace(workspace_id, user_id)
        workflow = await self._get_workflow_row(workspace_id, workflow_id)
        return await self._read_open_questions(workspace, workflow)

    async def submit_analyze_answers(
        self,
        workspace_id: UUID,
        workflow_id: UUID,
        user_id: UUID,
        answers: list[dict[str, str]],
    ) -> SpecPhase:
        """Fold the developer's answers into a remediation re-run of ``analyze``.

        The re-run edits spec.md/plan.md/tasks.md to resolve each answered finding, then
        regenerates analysis.md + open-questions.json (advisory: it never gates ``implement``).
        """
        workspace = await self._get_workspace(workspace_id, user_id)
        workflow = await self._get_workflow_row(workspace_id, workflow_id)
        phase = await self._get_phase_row(workflow_id, "analyze")
        if phase.status != "awaiting_review":
            raise SpecPhaseNotReviewableError("analyze")

        questions = await self._read_open_questions(workspace, workflow)
        feedback = self._format_analyze_feedback(answers, questions)

        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace_id,
            action="spec_analyze_answered",
            target_type="spec_phase",
            target_id=phase.id,
            metadata={"workflow_id": workflow_id, "answered": len(answers)},
        )
        await NotificationService(self.session).create(
            user_id=user_id,
            workspace_id=workspace_id,
            notification_type="spec_analyze_answered",
            title="Analyze answers submitted",
            message="Re-running 'analyze' to apply your answers.",
        )
        # Re-run analyze with the answers folded into the prompt as remediation feedback.
        return await self.run_phase(
            workspace_id, workflow_id, "analyze", user_id, feedback=feedback
        )

    @staticmethod
    def _format_analyze_feedback(
        answers: list[dict[str, str]], questions: list[dict]
    ) -> str:
        by_id = {str(q.get("id")): q for q in questions}
        lines = []
        for answer in answers:
            qid = str(answer.get("id", "")).strip()
            text = str(answer.get("answer", "")).strip()
            if not qid or not text:
                continue
            summary = str(by_id.get(qid, {}).get("summary", "")).strip()
            prefix = f"[{qid}] {summary}".strip()
            lines.append(f"- {prefix} → Answer: {text}")
        header = "Developer answers to the open questions from the previous analysis:\n\n"
        return header + "\n".join(lines)

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
        rel_path: str | None = None,
    ) -> tuple[str | None, str]:
        workspace = await self._get_workspace(workspace_id, user_id)
        workflow = await self._get_workflow_row(workspace_id, workflow_id)
        phase = await self._get_phase_row(workflow_id, phase_name)
        working_dir = self._working_dir(workspace, workflow, phase_name)
        if rel_path is not None:
            target = self._validate_spec_path(workflow, rel_path)
            content = await self._read_file(workspace, working_dir, target)
            return target, content or ""
        if not phase.artifact_path:
            return None, ""
        content = await self._read_file(workspace, working_dir, phase.artifact_path)
        return phase.artifact_path, content or ""

    async def list_phase_artifacts(
        self,
        workspace_id: UUID,
        workflow_id: UUID,
        phase_name: str,
        user_id: UUID,
    ) -> list[tuple[str, str]]:
        """Return ``(display_name, path)`` for every artifact file a phase has produced.

        For ``plan`` this includes the primary ``plan.md`` plus whichever design files exist
        (``research.md``, ``data-model.md``, ``contracts/*``, ``quickstart.md``). Other phases
        return their single artifact (when present).
        """
        if phase_name not in PHASE_PLANS:
            raise InvalidSpecPhaseError(phase_name)
        workspace = await self._get_workspace(workspace_id, user_id)
        workflow = await self._get_workflow_row(workspace_id, workflow_id)
        plan = PHASE_PLANS[phase_name]
        working_dir = self._working_dir(workspace, workflow, phase_name)

        items: list[tuple[str, str]] = []
        primary = plan.artifact.format(spec_dir=workflow.spec_dir) if plan.artifact else None
        if primary and await self._path_exists(workspace, working_dir, primary):
            items.append((primary.rsplit("/", 1)[-1], primary))

        for design in plan.design_artifacts:
            if design.endswith("/"):
                sub = design.rstrip("/")
                rel_dir = f"{workflow.spec_dir}/{sub}"
                for name in await self._list_dir(workspace, working_dir, rel_dir):
                    items.append((f"{sub}/{name}", f"{rel_dir}/{name}"))
            else:
                rel = f"{workflow.spec_dir}/{design}"
                if await self._path_exists(workspace, working_dir, rel):
                    items.append((design, rel))
        return items

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

        if phase_name == "plan":
            sections.append(self._plan_output_instructions(workflow.spec_dir))
        elif phase_name == "analyze":
            sections.append(
                self._analyze_output_instructions(workflow.spec_dir, feedback is not None)
            )
        elif artifact_path:
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

    @staticmethod
    def _plan_output_instructions(spec_dir: str) -> str:
        """Mirror Spec Kit's /plan Phase 0 + Phase 1: emit the full design set, not just plan.md."""
        return (
            "Produce the full GitHub Spec Kit plan design set, writing each file with the "
            "write_file tool. Do NOT run git or shell scripts; only read and write files.\n"
            f"1. `{spec_dir}/plan.md` — the completed implementation plan (fill the template "
            "above).\n"
            f"2. Phase 0 — `{spec_dir}/research.md`: for every unknown or clarification in the "
            "plan's Technical Context, record `Decision:` / `Rationale:` / `Alternatives "
            "considered:`.\n"
            f"3. Phase 1 — `{spec_dir}/data-model.md`: entities, fields, relationships, validation "
            "rules, and state transitions extracted from the spec.\n"
            f"4. Phase 1 — contract files under `{spec_dir}/contracts/` (one file per interface, "
            "e.g. OpenAPI/JSON Schema for APIs, command schemas for CLIs). Create this directory "
            "ONLY if the feature exposes external interfaces; skip it for purely internal "
            "features.\n"
            f"5. Phase 1 — `{spec_dir}/quickstart.md`: runnable validation steps that prove the "
            "feature end-to-end (prerequisites, setup, commands, expected outcomes). Reference the "
            "contracts and data model instead of duplicating them; do not include full "
            "implementation code.\n"
            "Write the complete content of each file; do not output the artifacts in chat."
        )

    @staticmethod
    def _analyze_output_instructions(spec_dir: str, remediating: bool) -> str:
        """Emit both the human report and a machine-readable open-questions list."""
        base = (
            "Cross-check spec.md, plan.md, and tasks.md for issues and write TWO files with the "
            "write_file tool (do NOT run git or shell scripts):\n"
            f"1. `{spec_dir}/analysis.md` — a human-readable report: a findings table with columns "
            "ID | Category | Severity | Location(s) | Summary | Recommendation, plus a coverage "
            "summary and metrics.\n"
            f"2. `{spec_dir}/open-questions.json` — a JSON array (machine-readable) where each "
            'item is `{"id": "A1", "category": "Ambiguity", "severity": "HIGH", "location": '
            '"spec.md:L20", "summary": "...", "recommendation": "..."}`. Category is one of '
            "Duplication, Ambiguity, Underspecification, Constitution, Coverage, Inconsistency. "
            "Severity is one of CRITICAL, HIGH, MEDIUM, LOW. Use the SAME ids as the report table. "
            "Write `[]` (an empty array) if there are no open questions."
        )
        if remediating:
            base += (
                "\n\nThe feedback above contains the developer's answers to specific findings. "
                "First EDIT the relevant artifacts (spec.md, plan.md, and/or tasks.md) to resolve "
                "each answered finding, then regenerate analysis.md and open-questions.json so the "
                "resolved findings are removed and only remaining or newly discovered ones are "
                "listed."
            )
        return base

    # ----- finalizer ----------------------------------------------------------------

    async def _finalize_phase(
        self,
        workflow_id: UUID,
        phase_name: str,
        run_id: UUID,
        user_id: UUID,
    ) -> None:
        """Fire-and-forget finalizer wrapper: never let an exception escape.

        ``_finalize_phase_inner`` commits the phase artifact before writing any status, so a
        real failure there (``git commit`` blocked by an index lock, a hook or a missing
        identity; a Daytona error) used to propagate out of a discarded task. The only trace
        was an "exception never retrieved" warning while ``phase.status`` and
        ``workflow.status`` stayed ``running`` — the UI then spun forever with no error and no
        notification. Record the failure the same way a non-completed run does instead.
        """
        try:
            await self._finalize_phase_inner(workflow_id, phase_name, run_id, user_id)
        except Exception as error:  # noqa: BLE001 - last line of defence for a bare task
            logger.exception(
                "spec_phase_finalize_failed",
                workflow_id=str(workflow_id),
                phase=phase_name,
                run_id=str(run_id),
            )
            await self._mark_phase_failed(workflow_id, phase_name, user_id, str(error))

    async def _mark_phase_failed(
        self,
        workflow_id: UUID,
        phase_name: str,
        user_id: UUID,
        detail: str,
    ) -> None:
        """Put a phase (and its workflow) into ``error`` and tell the user why."""
        try:
            async with self.session_maker() as session:
                workflow = await session.get(SpecWorkflow, workflow_id)
                phase = await self._load_phase(session, workflow_id, phase_name)
                if workflow is None or phase is None:
                    return
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
                    message=f"The '{phase_name}' phase could not be finalized: {detail}",
                )
        except Exception:  # noqa: BLE001 - nothing left to escalate to
            logger.exception(
                "spec_phase_error_record_failed",
                workflow_id=str(workflow_id),
                phase=phase_name,
            )

    async def _finalize_phase_inner(
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

            # Constitution-first fresh start: the constitution was auto-generated and ``specify``
            # has not run yet. Complete it without review and auto-chain ``specify`` below. A
            # manual re-run of constitution later (specify no longer pending) is unaffected and
            # still halts for review like any other REVIEW_PHASE.
            auto_chain_specify = False
            if phase_name == "constitution":
                specify_phase = await self._load_phase(session, workflow_id, "specify")
                auto_chain_specify = (
                    specify_phase is not None and specify_phase.status == "pending"
                )
            ws_id = workflow.workspace_id

            if auto_chain_specify:
                phase.status = "completed"
                workflow.status = "running"
            elif phase_name in REVIEW_PHASES:
                phase.status = "awaiting_review"
                workflow.status = "awaiting_review"
            elif phase_name == "analyze":
                # Advisory: halt for answers only while open questions remain; never gate
                # ``implement`` (analyze is absent from PREREQUISITES["implement"]).
                if await self._read_open_questions(workspace, workflow):
                    phase.status = "awaiting_review"
                    workflow.status = "awaiting_review"
                else:
                    phase.status = "completed"
                    workflow.status = "running"
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

        # Auto-chain: now that the constitution is in place, generate spec.md. Runs on a fresh
        # session/service because the request session is long gone; the constitution's agent run
        # is already terminal, so specify won't trip the one-active-run guard.
        if auto_chain_specify:
            async with self.session_maker() as chain_session:
                chained = SpecKitService(
                    chain_session, self.session_maker, self.settings, self.runtime
                )
                try:
                    await chained.run_phase(ws_id, workflow_id, "specify", user_id)
                except Exception:  # noqa: BLE001 - best-effort chain in a fire-and-forget task
                    workflow_row = await chain_session.get(SpecWorkflow, workflow_id)
                    specify_row = await self._load_phase(chain_session, workflow_id, "specify")
                    if workflow_row is not None:
                        workflow_row.status = "error"
                        chain_session.add(workflow_row)
                    if specify_row is not None:
                        specify_row.status = "error"
                        chain_session.add(specify_row)
                    await chain_session.commit()
                    if specify_row is not None:
                        await self._notify_and_audit(
                            chain_session,
                            workspace_id=ws_id,
                            user_id=user_id,
                            action="spec_phase_error",
                            phase=specify_row,
                            notification_type="spec_phase_error",
                            title="Spec phase failed",
                            message="Failed to auto-start the 'specify' phase after the "
                            "constitution was generated.",
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
        name = (
            user.name if user and user.name else (user.email if user else DEFAULT_COMMIT_AUTHOR)
        ) or DEFAULT_COMMIT_AUTHOR
        email = user.email if user else DEFAULT_COMMIT_EMAIL
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

    async def _constitution_ratified(self, workspace: Workspace) -> bool:
        """True only when constitution.md exists AND its placeholder tokens are filled in.

        ``specify init`` always scaffolds a placeholder ``.specify/memory/constitution.md``, so a
        plain existence check is meaningless; we treat the constitution as established only once
        ``/speckit.constitution`` has replaced the bracketed template tokens.
        """
        content = await self._read_file(
            workspace, workspace.workspace_path, PHASE_PLANS["constitution"].artifact
        )
        if not content or not content.strip():
            return False
        return CONSTITUTION_PLACEHOLDER.search(content) is None

    async def _list_dir(self, workspace: Workspace, cwd: str, rel_dir: str) -> list[str]:
        result = await self.runtime.exec(
            workspace,
            f"ls -1 {shlex.quote(rel_dir)}",
            cwd=cwd,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if result.exit_code != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    async def _read_open_questions(
        self, workspace: Workspace, workflow: SpecWorkflow
    ) -> list[dict]:
        working_dir = self._working_dir(workspace, workflow, "analyze")
        raw = await self._read_file(
            workspace, working_dir, f"{workflow.spec_dir}/open-questions.json"
        )
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _validate_spec_path(workflow: SpecWorkflow, rel_path: str) -> str:
        """Constrain an artifact request to files inside the workflow's spec directory."""
        candidate = rel_path.strip()
        prefix = workflow.spec_dir.rstrip("/") + "/"
        if (
            not candidate
            or candidate.startswith("/")
            or ".." in candidate.split("/")
            or not candidate.startswith(prefix)
        ):
            raise InvalidArtifactPathError(rel_path)
        return candidate

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
