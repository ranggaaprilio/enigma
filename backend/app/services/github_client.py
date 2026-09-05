"""GitHub Pull Request integration.

``GitHubClient`` is a thin async wrapper over the GitHub REST API (``httpx``);
``PullRequestService`` orchestrates token lookup, ``owner/repo`` parsing, the API call, and a
lightweight DB cache in ``pull_requests``. Provider-native pull requests are GitHub-only for now:
the workspace must be connected to a GitHub remote with an access token (an SSH deploy key
cannot call the REST API), otherwise ``PullRequestsUnsupportedError`` is raised. The stored token
is used server-side only and is never returned to the client; all surfaced output is run through
``sanitize_git_output`` to redact credentials.
"""

from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import PullRequest, RepositoryCredential, Workspace
from app.services.repositories import (
    PullRequestsUnsupportedError,
    RepositoryCredentialNotFoundError,
    RepositoryTokenError,
    sanitize_git_output,
)
from app.services.workspaces import WorkspaceNotFoundError, WorkspaceSecretCipher


class GitHubApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class PullRequestData:
    number: int
    url: str
    state: str
    title: str
    head: str
    base: str
    draft: bool
    merged: bool
    mergeable_state: str | None


def parse_owner_repo(repo_url: str) -> tuple[str, str]:
    """Derive ``(owner, repo)`` from an already-sanitized GitHub URL."""
    path = urlparse(repo_url).path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    owner, _, repo = path.partition("/")
    if not owner or not repo:
        raise GitHubApiError("Could not parse owner/repo from repository URL")
    return owner, repo


def to_pull_request_data(payload: dict) -> PullRequestData:
    head = ((payload.get("head") or {}).get("ref")) or ""
    base = ((payload.get("base") or {}).get("ref")) or ""
    return PullRequestData(
        number=int(payload.get("number", 0)),
        url=payload.get("html_url") or "",
        state="merged" if payload.get("merged") else (payload.get("state") or "open"),
        title=payload.get("title") or "",
        head=head,
        base=base,
        draft=bool(payload.get("draft", False)),
        merged=bool(payload.get("merged", False)),
        mergeable_state=payload.get("mergeable_state"),
    )


class GitHubClient:
    def __init__(
        self, settings: Settings, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.base_url = settings.github_api_base_url.rstrip("/")
        self.timeout = settings.github_api_timeout_seconds
        # An optional transport lets tests stub the GitHub API without real network calls.
        self.transport = transport

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Enigma",
        }

    async def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ):
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout, transport=self.transport
        ) as client:
            try:
                response = await client.request(
                    method, path, headers=self._headers(token), json=json, params=params
                )
            except httpx.HTTPError as error:
                raise GitHubApiError(sanitize_git_output(str(error), token)) from error
        if response.status_code >= 400:
            raise GitHubApiError(
                sanitize_git_output(self._error_message(response), token),
                status_code=response.status_code,
            )
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            data = response.json()
        except Exception:
            return f"GitHub API error ({response.status_code})"
        message = data.get("message") or "GitHub API error"
        errors = data.get("errors")
        if isinstance(errors, list):
            detail = "; ".join(
                str(item.get("message", "")) for item in errors if isinstance(item, dict)
            ).strip("; ")
            if detail:
                message = f"{message}: {detail}"
        return message

    async def create_pull_request(
        self,
        token: str,
        owner: str,
        repo: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str | None = None,
        draft: bool = False,
    ) -> dict:
        return await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            token,
            json={
                "title": title,
                "head": head,
                "base": base,
                "body": body or "",
                "draft": draft,
            },
        )

    async def get_pull_request(self, token: str, owner: str, repo: str, number: int) -> dict:
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}", token)

    async def list_pull_requests(
        self,
        token: str,
        owner: str,
        repo: str,
        *,
        head: str | None = None,
        base: str | None = None,
        state: str = "open",
    ) -> list[dict]:
        params: dict[str, str] = {"state": state}
        if head:
            params["head"] = f"{owner}:{head}"
        if base:
            params["base"] = base
        result = await self._request(
            "GET", f"/repos/{owner}/{repo}/pulls", token, params=params
        )
        return result if isinstance(result, list) else []


class PullRequestService:
    def __init__(
        self,
        session: AsyncSession,
        cipher: WorkspaceSecretCipher,
        settings: Settings,
        client: GitHubClient | None = None,
    ) -> None:
        self.session = session
        self.cipher = cipher
        self.settings = settings
        self.client = client or GitHubClient(settings)

    async def create(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        *,
        head: str,
        base: str | None,
        title: str,
        body: str | None,
        draft: bool,
        worktree_id: UUID | None = None,
    ) -> PullRequest:
        workspace = await self._get_workspace(workspace_id, owner_id)
        token = await self._require_github_token(workspace)
        owner, repo = parse_owner_repo(workspace.repo_url or "")
        base_ref = base or workspace.default_branch
        payload = await self.client.create_pull_request(
            token, owner, repo, head=head, base=base_ref, title=title, body=body, draft=draft
        )
        return await self._upsert(workspace_id, worktree_id, to_pull_request_data(payload))

    async def get(self, workspace_id: UUID, owner_id: UUID, number: int) -> PullRequest:
        workspace = await self._get_workspace(workspace_id, owner_id)
        token = await self._require_github_token(workspace)
        owner, repo = parse_owner_repo(workspace.repo_url or "")
        payload = await self.client.get_pull_request(token, owner, repo, number)
        cached = await self._find_cached(workspace_id, number)
        return await self._upsert(
            workspace_id,
            cached.worktree_id if cached else None,
            to_pull_request_data(payload),
        )

    async def list_open(self, workspace_id: UUID, owner_id: UUID) -> list[PullRequest]:
        await self._get_workspace(workspace_id, owner_id)
        result = await self.session.execute(
            select(PullRequest)
            .where(PullRequest.workspace_id == workspace_id)
            .order_by(PullRequest.created_at.desc())
        )
        return list(result.scalars().all())

    # ----- helpers ------------------------------------------------------------------

    async def _upsert(
        self, workspace_id: UUID, worktree_id: UUID | None, data: PullRequestData
    ) -> PullRequest:
        row = await self._find_cached(workspace_id, data.number)
        if row is None:
            row = PullRequest(workspace_id=workspace_id, number=data.number)
            self.session.add(row)
        if worktree_id is not None:
            row.worktree_id = worktree_id
        row.url = data.url
        row.state = data.state
        row.head = data.head
        row.base = data.base
        row.title = data.title
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def _find_cached(self, workspace_id: UUID, number: int) -> PullRequest | None:
        result = await self.session.execute(
            select(PullRequest).where(
                PullRequest.workspace_id == workspace_id,
                PullRequest.number == number,
            )
        )
        return result.scalar_one_or_none()

    async def _get_workspace(self, workspace_id: UUID, owner_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != owner_id:
            raise WorkspaceNotFoundError
        return workspace

    async def _require_github_token(self, workspace: Workspace) -> str:
        """The GitHub access token for ``workspace``, or why PRs are unavailable for it."""
        if not workspace.repo_url:
            raise RepositoryCredentialNotFoundError
        result = await self.session.execute(
            select(RepositoryCredential).where(
                RepositoryCredential.workspace_id == workspace.id,
            )
        )
        credential = result.scalar_one_or_none()
        if credential is None:
            raise RepositoryCredentialNotFoundError
        if credential.provider != "github" or credential.auth_type != "personal_access_token":
            raise PullRequestsUnsupportedError(credential.provider, credential.auth_type)
        token = self.cipher.decrypt(credential.encrypted_token or "")
        if token is None:
            raise RepositoryTokenError
        return token
