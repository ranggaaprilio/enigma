"""local-docker-runtime Epic 06: ``daytona_sandbox_id`` -> ``sandbox_id``, contract done.

0037 added the column, 0038 moved reads to it, 0039 dropped the old one and moved its
uniqueness to ``ix_workspaces_sandbox_id``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models import Workspace


def test_the_old_column_is_gone() -> None:
    assert "daytona_sandbox_id" not in Workspace.__table__.c


def test_queries_by_sandbox_id_use_the_new_column() -> None:
    sql = str(
        select(Workspace.id)
        .where(Workspace.sandbox_id == "x")
        .compile(dialect=postgresql.dialect())
    )
    assert "workspaces.sandbox_id =" in sql


def test_the_column_and_its_unique_index_are_mapped() -> None:
    table = Workspace.__table__
    assert table.c.sandbox_id.nullable is True
    assert any(
        index.name == "ix_workspaces_sandbox_id"
        and index.unique
        and list(index.columns) == [table.c.sandbox_id]
        for index in table.indexes
    )
