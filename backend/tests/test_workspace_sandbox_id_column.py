"""local-docker-runtime Epic 06: ``daytona_sandbox_id`` -> ``sandbox_id``, expand half.

During the expand release both columns are written; reads stay on ``daytona_sandbox_id``,
which the previous release still writes on its own while old and new replicas overlap. The
contract release backfills once more, switches reads to ``sandbox_id`` and drops the old
column (``# contract:``).
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


def test_reads_stay_on_the_old_column_while_old_replicas_may_write_it() -> None:
    workspace = Workspace(id=uuid4(), owner_id=uuid4(), name="w", slug="w")
    workspace.daytona_sandbox_id = "written-by-the-previous-release"

    assert workspace.sandbox_id == "written-by-the-previous-release"


def test_queries_by_sandbox_id_use_the_old_column() -> None:
    sql = str(
        select(Workspace.id)
        .where(Workspace.sandbox_id == "x")
        .compile(dialect=postgresql.dialect())
    )
    assert "workspaces.daytona_sandbox_id =" in sql


def test_the_new_column_and_its_index_are_mapped() -> None:
    table = Workspace.__table__
    assert table.c.sandbox_id.nullable is True
    assert any(
        index.name == "ix_workspaces_sandbox_id" and list(index.columns) == [table.c.sandbox_id]
        for index in table.indexes
    )
