from pydantic import BaseModel, Field, field_validator


def normalize_nonempty(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("Value cannot be empty")
    return stripped


class RepositoryConnectRequest(BaseModel):
    repo_url: str = Field(min_length=1, max_length=2048)
    token: str = Field(min_length=1, max_length=20_000)

    @field_validator("repo_url", "token")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return normalize_nonempty(value)


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
