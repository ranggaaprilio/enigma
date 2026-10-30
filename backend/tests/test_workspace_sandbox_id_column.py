"""local-docker-runtime Epic 06: ``daytona_sandbox_id`` -> ``sandbox_id``.

0037 added the column and wrote both; 0038 backfills once more and moves reads to
``sandbox_id``. Both columns are still written, because the 0037 release reads the old one
while it overlaps this one's rollout. 0039 drops the old column (``# contract:``).
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models import Workspace


def test_setting_the_sandbox_id_writes_both_columns() -> None:
    workspace = Workspace(id=uuid4(), owner_id=uuid4(), name="w", slug="w")

    workspace.sandbox_id = "container-1"

    assert workspace.daytona_sandbox_id == "container-1"
    assert workspace.sandbox_id_column == "container-1"
    assert workspace.sandbox_id == "container-1"


def test_reads_use_the_new_column() -> None:
    workspace = Workspace(id=uuid4(), owner_id=uuid4(), name="w", slug="w")
    workspace.sandbox_id_column = "new"
    workspace.daytona_sandbox_id = "old"

    assert workspace.sandbox_id == "new"


def test_queries_by_sandbox_id_use_the_new_column() -> None:
    sql = str(
        select(Workspace.id)
        .where(Workspace.sandbox_id == "x")
        .compile(dialect=postgresql.dialect())
    )
    assert "workspaces.sandbox_id =" in sql


def test_the_new_column_and_its_index_are_mapped() -> None:
    table = Workspace.__table__
    assert table.c.sandbox_id.nullable is True
    assert any(
        index.name == "ix_workspaces_sandbox_id" and list(index.columns) == [table.c.sandbox_id]
        for index in table.indexes
    )
