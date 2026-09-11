"""Epic 13: the composed preview descriptor -- one endpoint listing every service with its
role, port, health, both preview URL forms, and workspace-level advisories, so the frontend
renders one coherent view instead of stitching together `/services`, `/preview/sessions`, and
guesswork.

Composes existing services rather than duplicating them: `WorkspaceServiceManager.list` (rows +
probe reconciliation), `to_response_list` (the per-service response dict), `PreviewAliasService`
(stable URLs), and `ServiceDiagnosticsService` (unresolved CORS findings). The one additive
change elsewhere is `WorkspaceServiceManager.list(..., probe=False)`, used here because this
endpoint is polled every few seconds by the frontend and must cost zero sandbox round-trips by
default.

Advisories are *computed on every read*, never stored -- unlike `service_diagnostics`, which
records *events* the proxy cannot re-derive later. A "no dev proxy configured" advisory has no
natural invalidation point (a file edit, a worktree switch, a registry change all change the
answer with no event to hook), so storing it would go stale silently. The rule: events are
stored, derivations are computed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from app.core.config import Settings
from app.models import CustomCommand, Workspace
from app.services.env_templates import references as template_references
from app.services.preview_alias import PreviewAliasService, alias_prime_url, alias_url
from app.services.preview_capability import capability_from_host
from app.services.run_detection import framework_signature
from app.services.service_diagnostics import ServiceDiagnosticsService
from app.services.workspace_env import load_all_values
from app.services.workspace_services import WorkspaceServiceManager

# Advisories older than this are not worth re-surfacing from `service_diagnostics` even though
# the row itself is retained for longer (`service_diagnostic_retention_days`).
_DIAGNOSIS_ADVISORY_WINDOW = timedelta(hours=24)

_WEB_SIGNATURES = frozenset({"vite", "next", "react-scripts", "nuxt", "remix", "ng serve"})
_API_SIGNATURES = frozenset(
    {
        "uvicorn",
        "fastapi",
        "manage.py runserver",
        "gunicorn",
        "flask",
        "artisan serve",
        "spring-boot",
        "bootrun",
        "rails",
    }
)
_WEB_PORTS = frozenset({5173, 3000, 4200, 4321, 1420})
_API_PORTS = frozenset({8000, 5000, 3001, 4000})

ServiceClass = Literal["web", "api", "other"]


def classify_service(*, command: str | None, port: int | None) -> ServiceClass:
    """web/api/other, from `run_detection.framework_signature` first (one source of truth for
    "what does this command run", shared with proxy-config attribution) and port only as a
    tiebreak. Port 8080 is deliberately ambiguous on its own -- both a plain web server and
    Spring Boot/Tomcat use it, and the latter already has its own signature."""
    if command:
        signature = framework_signature(command)
        if signature in _WEB_SIGNATURES:
            return "web"
        if signature in _API_SIGNATURES:
            return "api"
    if port in _WEB_PORTS:
        return "web"
    if port in _API_PORTS:
        return "api"
    return "other"


def entry_point_service(
    services: list[dict],
) -> tuple[UUID | None, Literal["explicit", "inferred"]]:
    """Falls back through, in order: an explicit `is_entry_point` row; the single `role='dev'`
    service; the single running service classified "web"; the only running service at all.
    Never requires a workspace to have one marked -- a workspace with nothing running yet
    simply has no entry point."""
    explicit = next((s for s in services if s.get("is_entry_point")), None)
    if explicit is not None:
        return explicit["id"], "explicit"

    dev_role = [s for s in services if s.get("role") == "dev"]
    if len(dev_role) == 1:
        return dev_role[0]["id"], "inferred"

    running = [s for s in services if s.get("status") == "running"]
    running_web = [
        s for s in running
        if classify_service(command=s.get("command"), port=s.get("port")) == "web"
    ]
    if len(running_web) == 1:
        return running_web[0]["id"], "inferred"
    if len(running) == 1:
        return running[0]["id"], "inferred"
    return None, "inferred"


@dataclass(frozen=True)
class WorkspaceAdvisory:
    code: str
    severity: Literal["info", "warning", "error"]
    title: str
    detail: str
    framework: str | None = None
    snippet: str | None = None
    snippet_language: str | None = None
    target_file: str | None = None
    service_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class ComposedService:
    service_id: UUID
    custom_command_id: UUID | None
    name: str
    role: str
    kind: str
    port: int | None
    status: str
    health_status: str
    health_checked_at: datetime | None
    is_entry_point: bool
    entry_point_source: Literal["explicit", "inferred"]
    branch: str | None
    worktree_id: UUID | None
    preview_session_id: UUID | None
    capability_url: str | None
    capability_url_rotates: bool
    alias: str | None
    alias_url: str | None
    alias_prime_url: str | None
    localhost_url: str | None
    diagnoses: list = field(default_factory=list)


@dataclass(frozen=True)
class PreviewComposition:
    workspace_id: UUID
    preview_alias_enabled: bool
    proxy_base_url: str
    entry_point_service_id: UUID | None
    services: list[ComposedService]
    advisories: list[WorkspaceAdvisory]


class PreviewCompositionService:
    def __init__(
        self,
        session,
        settings: Settings,
        service_manager: WorkspaceServiceManager | None = None,
        alias_service: PreviewAliasService | None = None,
        diagnostics_service: ServiceDiagnosticsService | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.service_manager = service_manager or WorkspaceServiceManager(session, settings)
        self.alias_service = alias_service or PreviewAliasService(session, settings)
        self.diagnostics_service = diagnostics_service or ServiceDiagnosticsService(session)

    async def compose(
        self, workspace_id: UUID, user_id: UUID, *, check_proxy: bool = False
    ) -> PreviewComposition:
        rows, _primary = await self.service_manager.list(workspace_id, user_id, probe=False)
        workspace = await self.service_manager.get_workspace_for(workspace_id, user_id)

        if workspace.preview_alias_enabled:
            # Defensive: a service command created/enabled after aliases were turned on gets
            # its alias here rather than waiting for another `enable()` call.
            await self.alias_service.ensure_for_workspace(workspace)

        services_data = await self.service_manager.to_response_list(workspace, rows)
        entry_point_id, entry_point_source = entry_point_service(services_data)

        since = datetime.now(UTC) - _DIAGNOSIS_ADVISORY_WINDOW
        diagnoses_by_service = await self._diagnoses_by_service(workspace_id, since=since)

        composed: list[ComposedService] = []
        cors_service_ids: list[UUID] = []
        for data in services_data:
            service_diagnoses = diagnoses_by_service.get(data["id"], [])
            if service_diagnoses:
                cors_service_ids.append(data["id"])
            composed.append(
                await self._compose_one(
                    workspace, data, entry_point_id, entry_point_source, service_diagnoses
                )
            )

        advisories = await self._advisories(
            workspace,
            services_data,
            composed,
            cors_service_ids=cors_service_ids,
            check_proxy=check_proxy,
        )

        return PreviewComposition(
            workspace_id=workspace.id,
            preview_alias_enabled=bool(workspace.preview_alias_enabled),
            proxy_base_url=self.settings.daytona_preview_proxy_base_url,
            entry_point_service_id=entry_point_id,
            services=composed,
            advisories=advisories,
        )

    async def _compose_one(
        self,
        workspace: Workspace,
        data: dict,
        entry_point_id: UUID | None,
        entry_point_source: Literal["explicit", "inferred"],
        diagnoses: list,
    ) -> ComposedService:
        capability_value: str | None = None
        if data.get("public_url"):
            try:
                netloc = data["public_url"].split("//", 1)[-1].split("/", 1)[0]
                capability_value = capability_from_host(
                    netloc, base_url=self.settings.daytona_preview_proxy_base_url
                )
            except Exception:  # noqa: BLE001 - a malformed public_url just means no prime url
                capability_value = None

        alias_row = None
        alias_url_value: str | None = None
        alias_prime_url_value: str | None = None
        if workspace.preview_alias_enabled:
            alias_row = await self.alias_service.alias_for_row(
                workspace.id, data.get("custom_command_id")
            )
            if alias_row is not None and alias_row.enabled:
                alias_url_value = alias_url(
                    alias_row.alias, base_url=self.settings.daytona_preview_proxy_base_url
                )
                if capability_value:
                    alias_prime_url_value = alias_prime_url(
                        alias_row.alias,
                        base_url=self.settings.daytona_preview_proxy_base_url,
                        capability=capability_value,
                        capability_query_param=self.settings.preview_alias_capability_query_param,
                    )

        localhost_url = f"http://127.0.0.1:{data['port']}" if data.get("port") else None

        return ComposedService(
            service_id=data["id"],
            custom_command_id=data.get("custom_command_id"),
            name=data["name"],
            role=data["role"],
            kind=data["kind"],
            port=data.get("port"),
            status=data["status"],
            health_status=data.get("health_status", "unknown"),
            health_checked_at=data.get("health_checked_at"),
            is_entry_point=data["id"] == entry_point_id,
            entry_point_source=entry_point_source,
            branch=data.get("branch"),
            worktree_id=data.get("worktree_id"),
            preview_session_id=data.get("preview_session_id"),
            capability_url=data.get("public_url"),
            capability_url_rotates=True,
            alias=alias_row.alias if alias_row is not None else None,
            alias_url=alias_url_value,
            alias_prime_url=alias_prime_url_value,
            localhost_url=localhost_url,
            diagnoses=diagnoses,
        )

    async def _diagnoses_by_service(
        self, workspace_id: UUID, *, since: datetime
    ) -> dict[UUID, list]:
        rows = await self.diagnostics_service.for_workspace(workspace_id, since=since)
        by_service: dict[UUID, list] = {}
        for row in rows:
            by_service.setdefault(row.service_id, []).append(row)
        return by_service

    async def _advisories(
        self,
        workspace: Workspace,
        services_data: list[dict],
        composed: list[ComposedService],
        *,
        cors_service_ids: list[UUID],
        check_proxy: bool,
    ) -> list[WorkspaceAdvisory]:
        advisories: list[WorkspaceAdvisory] = []

        if cors_service_ids:
            advisories.append(
                WorkspaceAdvisory(
                    code="cors_rejected",
                    severity="error",
                    title="A cross-origin request was rejected",
                    detail=(
                        "One or more services rejected a preview request from another origin. "
                        "See each service's diagnoses for the offending origin and the header "
                        "the API must send."
                    ),
                    service_ids=tuple(cors_service_ids),
                )
            )

        if not any(s.is_entry_point for s in composed) and services_data:
            advisories.append(
                WorkspaceAdvisory(
                    code="entry_point_unset",
                    severity="info",
                    title="No entry point set",
                    detail=(
                        "Mark a service as the entry point so the preview surface can show one "
                        "primary \"Open app\" button."
                    ),
                )
            )

        template_advisory = await self._template_unresolved_advisory(workspace, services_data)
        if template_advisory is not None:
            advisories.append(template_advisory)

        if check_proxy:
            proxy_advisory = await self._dev_proxy_advisory(workspace, services_data)
            if proxy_advisory is not None:
                advisories.append(proxy_advisory)

        return advisories

    async def _template_unresolved_advisory(
        self, workspace: Workspace, services_data: list[dict]
    ) -> WorkspaceAdvisory | None:
        values = await load_all_values(self.session, self.settings, workspace.id)
        refs = template_references(values)
        if not refs:
            return None
        # A reference is only worth flagging if it can't possibly resolve -- i.e. aliases are
        # disabled (every ENIGMA_PREVIEW_* reference needs them) -- never the value itself.
        if workspace.preview_alias_enabled:
            return None
        keys = sorted({ref.key for ref in refs})
        return WorkspaceAdvisory(
            code="template_unresolved",
            severity="warning",
            title="A template needs stable preview aliases",
            detail=(
                f"{', '.join(keys)} reference a preview URL template, but stable aliases are "
                "disabled for this workspace. Enable them, or these services will fail to "
                "start."
            ),
        )

    async def _dev_proxy_advisory(
        self, workspace: Workspace, services_data: list[dict]
    ) -> WorkspaceAdvisory | None:
        web = [
            s for s in services_data
            if classify_service(command=s.get("command"), port=s.get("port")) == "web"
        ]
        api = [
            s for s in services_data
            if classify_service(command=s.get("command"), port=s.get("port")) == "api"
        ]
        if not web or not api:
            return None

        from app.services.proxy_detection import snippet_for
        from app.services.run_detection import RunDetectionService

        detection = RunDetectionService(self.session, self.settings)
        service_cwds: dict[str, str | None] = {}
        for s in web:
            command_id = s.get("custom_command_id")
            command = await self.session.get(CustomCommand, command_id) if command_id else None
            service_cwds[str(s["id"])] = getattr(command, "cwd", None)

        try:
            results = await detection.read_proxy_configs(
                workspace.id, workspace.owner_id, worktree_id=None, service_cwds=service_cwds
            )
        except Exception:  # noqa: BLE001 - detection failing must never break composition
            return None

        any_configured = any(
            config.configured for configs in results.values() for config in configs
        )
        if any_configured:
            return None

        api_port = api[0].get("port") or 8000
        framework = None
        for configs in results.values():
            if configs:
                framework = configs[0].framework
                break
        framework = framework or "vite"
        return WorkspaceAdvisory(
            code="dev_proxy_missing",
            severity="warning",
            title="No dev-server proxy configured",
            detail=(
                "Your frontend's own dev server has no proxy to the API, so `/api/...` calls "
                "from the browser cannot reach localhost -- only the signed preview origin is "
                "reachable from there."
            ),
            framework=framework,
            snippet=snippet_for(framework, api_port=api_port),
            snippet_language="ts" if framework in ("vite", "nuxt") else "js",
            service_ids=tuple(UUID(key) for key in service_cwds),
        )
