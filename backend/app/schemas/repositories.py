from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GitProviderChoice = Literal["auto", "github", "gitlab", "bitbucket", "generic"]
GitProviderSlug = Literal["github", "gitlab", "bitbucket", "generic"]
RepositoryAuthType = Literal["personal_access_token", "ssh_key"]


def normalize_nonempty(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("Value cannot be empty")
    return stripped


class RepositoryConnectRequest(BaseModel):
    repo_url: str = Field(min_length=1, max_length=2048)
    # ``auto`` detects the provider from the URL host; an explicit value is needed for self-hosted
    # GitLab (any host) so the right basic-auth username is used.
    provider: GitProviderChoice = "auto"
    auth_type: RepositoryAuthType = "personal_access_token"
    # Required for ``personal_access_token``; must be omitted for ``ssh_key`` (the key pair is
    # generated server-side and never uploaded).
    token: str | None = Field(default=None, max_length=20_000)
    # Optional basic-auth username override for token auth (Bitbucket app passwords, generic
    # hosts). Ignored for SSH.
    username: str | None = Field(default=None, max_length=255)

    @field_validator("repo_url")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return normalize_nonempty(value)

    @field_validator("token", "username")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def check_material_matches_auth_type(self) -> "RepositoryConnectRequest":
        if self.auth_type == "personal_access_token" and not self.token:
            raise ValueError("token is required for personal_access_token authentication")
        if self.auth_type == "ssh_key" and (self.token or self.username):
            raise ValueError("token and username must be omitted for ssh_key authentication")
        return self


class SshKeyRequest(BaseModel):
    # Replace an existing key pair (the old deploy key stops working immediately).
    rotate: bool = False


class RepositoryCredentialResponse(BaseModel):
    provider: GitProviderSlug
    auth_type: RepositoryAuthType
    username: str | None = None
    # Present for ``ssh_key`` credentials; add it as a deploy key with write access on the host.
    public_key: str | None = None
    supports_pull_requests: bool = False
    repo_url: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RepositoryFileStatus(BaseModel):
    path: str
    status: str


class RepositoryStatusResponse(BaseModel):
    cloned: bool
    branch: str | None
    is_clean: bool
    files: list[RepositoryFileStatus]


class RepositoryDiffResponse(BaseModel):
    diff: str


class GitCommitResponse(BaseModel):
    hash: str
    short_hash: str
    author_name: str
    author_email: str
    date: str
    subject: str


class RepositoryLogResponse(BaseModel):
    commits: list[GitCommitResponse]


class RepositoryCommitDetailResponse(BaseModel):
    hash: str
    short_hash: str
    author_name: str
    author_email: str
    date: str
    subject: str
    body: str
    diff: str


class RepositoryCommitRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        return normalize_nonempty(value)


class RepositoryDiscardRequest(BaseModel):
    # A specific file to discard; when omitted, discard all uncommitted changes.
    path: str | None = Field(default=None, max_length=1024)

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class RepositoryOperationResponse(BaseModel):
    ok: bool
    message: str
