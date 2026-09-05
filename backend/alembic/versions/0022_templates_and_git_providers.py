"""workspace templates, blank workspaces, multi-provider git credentials

Revision ID: 0022_templates_and_git_providers
Revises: 0021_comments
Create Date: 2026-09-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert

from alembic import op

revision: str = "0022_templates_and_git_providers"
down_revision: str | None = "0021_comments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _node_mise(dev_command: str) -> str:
    return (
        '[tools]\nnode = "20"\n\n'
        '[tasks.install]\nrun = "npm install"\n\n'
        f'[tasks.dev]\nrun = "{dev_command}"\n'
    )


def _python_mise(dev_command: str) -> str:
    return (
        '[tools]\npython = "3.12"\n\n'
        '[tasks.install]\nrun = "pip install -r requirements.txt"\n\n'
        f'[tasks.dev]\nrun = "{dev_command}"\n'
    )


# Built-in starter catalog. Fixed ids so tests and docs can reference them; ``ON CONFLICT (slug)``
# keeps re-running the migration idempotent. The sources are GitHub's public, standalone
# Codespaces starters; ``mise_config`` is written into the workspace when the template ships no
# mise.toml so the dev-server preview has a ``dev`` task to run.
SEED_TEMPLATES = [
    {
        "id": "00000000-0000-4000-8000-000000000001",
        "slug": "react-vite",
        "name": "React",
        "description": "A React single-page app with a hot-reloading dev server.",
        "source_repo_url": "https://github.com/github/codespaces-react.git",
        "source_ref": None,
        "default_branch": "main",
        "icon": "react",
        "tags": ["javascript", "react", "frontend"],
        "mise_config": _node_mise("npm start"),
        "preview_port": 3000,
        "sort_order": 10,
    },
    {
        "id": "00000000-0000-4000-8000-000000000002",
        "slug": "nextjs",
        "name": "Next.js",
        "description": "A full-stack React framework with file-based routing.",
        "source_repo_url": "https://github.com/github/codespaces-nextjs.git",
        "source_ref": None,
        "default_branch": "main",
        "icon": "nextjs",
        "tags": ["javascript", "react", "fullstack"],
        "mise_config": _node_mise("npm run dev"),
        "preview_port": 3000,
        "sort_order": 20,
    },
    {
        "id": "00000000-0000-4000-8000-000000000003",
        "slug": "node-express",
        "name": "Node.js + Express",
        "description": "A minimal HTTP API server on Express.",
        "source_repo_url": "https://github.com/github/codespaces-express.git",
        "source_ref": None,
        "default_branch": "main",
        "icon": "node",
        "tags": ["javascript", "node", "api"],
        "mise_config": _node_mise("npm start"),
        "preview_port": 3000,
        "sort_order": 30,
    },
    {
        "id": "00000000-0000-4000-8000-000000000004",
        "slug": "python-flask",
        "name": "Python + Flask",
        "description": "A small Flask web application.",
        "source_repo_url": "https://github.com/github/codespaces-flask.git",
        "source_ref": None,
        "default_branch": "main",
        "icon": "python",
        "tags": ["python", "flask", "web"],
        "mise_config": _python_mise(
            "python -m flask --app app run --host 0.0.0.0 --port 5000"
        ),
        "preview_port": 5000,
        "sort_order": 40,
    },
    {
        "id": "00000000-0000-4000-8000-000000000005",
        "slug": "python-django",
        "name": "Python + Django",
        "description": "A Django project with the admin and ORM ready to go.",
        "source_repo_url": "https://github.com/github/codespaces-django.git",
        "source_ref": None,
        "default_branch": "main",
        "icon": "python",
        "tags": ["python", "django", "fullstack"],
        "mise_config": _python_mise("python manage.py runserver 0.0.0.0:8000"),
        "preview_port": 8000,
        "sort_order": 50,
    },
]


def upgrade() -> None:
    # 1. workspace_templates
    templates = op.create_table(
        "workspace_templates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_repo_url", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.String(length=120), nullable=True),
        sa.Column(
            "default_branch", sa.String(length=120), nullable=False, server_default="main"
        ),
        sa.Column("icon", sa.String(length=80), nullable=True),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.String(length=40)),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
        sa.Column("mise_config", sa.Text(), nullable=True),
        sa.Column("preview_port", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("slug", name="uq_workspace_templates_slug"),
    )
    op.create_index(
        "ix_workspace_templates_active_sort",
        "workspace_templates",
        ["is_active", "sort_order"],
    )

    # 2. seed the built-in catalog
    op.execute(
        pg_insert(templates)
        .values(SEED_TEMPLATES)
        .on_conflict_do_nothing(index_elements=["slug"])
    )

    # 3. workspaces: blank/template workspaces have no remote yet
    op.alter_column("workspaces", "repo_url", existing_type=sa.Text(), nullable=True)
    op.add_column(
        "workspaces",
        sa.Column(
            "source", sa.String(length=20), nullable=False, server_default="repository"
        ),
    )
    op.create_check_constraint(
        "ck_workspaces_source",
        "workspaces",
        "source in ('repository', 'template', 'blank')",
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_workspaces_template_id", "workspaces", ["template_id"])

    # 4. repository_credentials: provider + auth type + SSH key material
    op.alter_column(
        "repository_credentials", "encrypted_token", existing_type=sa.Text(), nullable=True
    )
    op.add_column(
        "repository_credentials", sa.Column("encrypted_private_key", sa.Text(), nullable=True)
    )
    op.add_column("repository_credentials", sa.Column("public_key", sa.Text(), nullable=True))
    op.add_column(
        "repository_credentials", sa.Column("username", sa.String(length=255), nullable=True)
    )
    op.create_check_constraint(
        "ck_repository_credentials_provider",
        "repository_credentials",
        "provider in ('github', 'gitlab', 'bitbucket', 'generic')",
    )
    op.create_check_constraint(
        "ck_repository_credentials_auth_type",
        "repository_credentials",
        "auth_type in ('personal_access_token', 'ssh_key')",
    )
    op.create_check_constraint(
        "ck_repository_credentials_material",
        "repository_credentials",
        "(auth_type = 'personal_access_token' AND encrypted_token IS NOT NULL) OR "
        "(auth_type = 'ssh_key' AND encrypted_private_key IS NOT NULL AND public_key IS NOT NULL)",
    )


def downgrade() -> None:
    # 4. repository_credentials
    op.drop_constraint(
        "ck_repository_credentials_material", "repository_credentials", type_="check"
    )
    op.drop_constraint(
        "ck_repository_credentials_auth_type", "repository_credentials", type_="check"
    )
    op.drop_constraint(
        "ck_repository_credentials_provider", "repository_credentials", type_="check"
    )
    op.execute("DELETE FROM repository_credentials WHERE auth_type = 'ssh_key'")
    op.drop_column("repository_credentials", "username")
    op.drop_column("repository_credentials", "public_key")
    op.drop_column("repository_credentials", "encrypted_private_key")
    op.alter_column(
        "repository_credentials", "encrypted_token", existing_type=sa.Text(), nullable=False
    )

    # 3. workspaces
    op.drop_index("ix_workspaces_template_id", table_name="workspaces")
    op.drop_column("workspaces", "template_id")
    op.drop_constraint("ck_workspaces_source", "workspaces", type_="check")
    op.drop_column("workspaces", "source")
    op.execute("UPDATE workspaces SET repo_url = '' WHERE repo_url IS NULL")
    op.alter_column("workspaces", "repo_url", existing_type=sa.Text(), nullable=False)

    # 1. workspace_templates
    op.drop_index("ix_workspace_templates_active_sort", table_name="workspace_templates")
    op.drop_table("workspace_templates")
