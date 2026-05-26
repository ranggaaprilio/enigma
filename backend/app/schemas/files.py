from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

FileNodeType = Literal["file", "directory"]


def normalize_path(value: str) -> str:
    return value.strip().replace("\\", "/").strip("/")


class FileNode(BaseModel):
    path: str
    name: str
    type: FileNodeType
    size: int | None
    modified_at: datetime
    children: list["FileNode"] = Field(default_factory=list)


class FileReadResponse(BaseModel):
    path: str
    name: str
    type: Literal["file"]
    size: int
    modified_at: datetime
    content: str


class FileWriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=1_000_000)

    @field_validator("path")
    @classmethod
    def normalize_target_path(cls, value: str) -> str:
        return normalize_path(value)


class FolderCreateRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)

    @field_validator("path")
    @classmethod
    def normalize_target_path(cls, value: str) -> str:
        return normalize_path(value)


class FileRenameRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    new_path: str = Field(min_length=1, max_length=4096)

    @field_validator("path", "new_path")
    @classmethod
    def normalize_target_path(cls, value: str) -> str:
        return normalize_path(value)


class FileOperationResponse(BaseModel):
    ok: bool
    path: str
