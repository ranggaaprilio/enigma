"""unique spec workflow feature slug per workspace

A workflow's ``feature_slug`` is also its git branch name and its ``specs/<slug>``
directory, so two workflows sharing one would write over each other on disk. Duplicates
that already exist are re-numbered above the current maximum before the constraint is
added, so the migration cannot fail on legacy data.

Revision ID: 0023_spec_workflow_slug_unique
Revises: 0022_templates_and_git_providers
Create Date: 2026-09-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_spec_workflow_slug_unique"
down_revision: str | None = "0022_templates_and_git_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Re-number every duplicate but the oldest, keeping the "NNN-" prefix shape. The new
    # number continues above the workspace's current highest prefix.
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    workspace_id,
                    feature_slug,
                    ROW_NUMBER() OVER (
                        PARTITION BY workspace_id, feature_slug ORDER BY created_at, id
                    ) AS dup_rank
                FROM spec_workflows
            ),
            peak AS (
                SELECT
                    workspace_id,
                    COALESCE(
                        MAX(NULLIF(regexp_replace(feature_slug, '-.*$', ''), '')::int),
                        0
                    ) AS highest
                FROM spec_workflows
                WHERE feature_slug ~ '^[0-9]+'
                GROUP BY workspace_id
            ),
            renumbered AS (
                SELECT
                    ranked.id,
                    lpad(
                        (
                            COALESCE(peak.highest, 0)
                            + ROW_NUMBER() OVER (
                                PARTITION BY ranked.workspace_id ORDER BY ranked.id
                            )
                        )::text,
                        3,
                        '0'
                    )
                    || '-'
                    || regexp_replace(ranked.feature_slug, '^[0-9]+-', '') AS new_slug
                FROM ranked
                LEFT JOIN peak ON peak.workspace_id = ranked.workspace_id
                WHERE ranked.dup_rank > 1
            )
            UPDATE spec_workflows AS w
            SET feature_slug = renumbered.new_slug
            FROM renumbered
            WHERE w.id = renumbered.id
            """
        )
    )
    op.create_unique_constraint(
        "uq_spec_workflows_workspace_feature_slug",
        "spec_workflows",
        ["workspace_id", "feature_slug"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_spec_workflows_workspace_feature_slug",
        "spec_workflows",
        type_="unique",
    )
