"""Guards against model/migration constraint-name drift.

``Base`` is a bare ``DeclarativeBase`` with no SQLAlchemy naming convention (see
``app/db/base.py``), so every constraint and index name in a model's ``__table_args__`` is
hand-written and must match the migration that actually creates it, character for character.
This is a pure text check -- no database required -- so it catches drift the moment either
file is edited, without needing a live Postgres to run alongside it.
"""

import re
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "0027_service_wiring.py"
)

# Constraint/index names appear either as a bare string literal (positional first arg to
# ``op.create_check_constraint``/``op.create_index``) or as a ``name="..."`` kwarg (inside
# ``sa.CheckConstraint``/``sa.UniqueConstraint``/``sa.Index``), so match any quoted string that
# looks like one of this codebase's hand-written names rather than requiring a specific call
# shape.
_NAME_PATTERN = re.compile(r"""['"]((?:ck|uq|ix|fk)_[a-zA-Z0-9_]+)['"]""")


def _names_in(path: Path) -> set[str]:
    with open(path) as fh:
        content = fh.read()
    return set(_NAME_PATTERN.findall(content))


def test_workspace_preview_alias_constraint_names_match_migration():
    model_path = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "models"
        / "workspace_preview_alias.py"
    )
    model_names = _names_in(model_path)
    migration_names = _names_in(MIGRATION_PATH)
    missing = model_names - migration_names
    assert not missing, f"Names in model but not migration: {missing}"


def test_service_diagnostic_constraint_names_match_migration():
    model_path = (
        Path(__file__).resolve().parent.parent / "app" / "models" / "service_diagnostic.py"
    )
    model_names = _names_in(model_path)
    migration_names = _names_in(MIGRATION_PATH)
    missing = model_names - migration_names
    assert not missing, f"Names in model but not migration: {missing}"


def test_command_and_workspace_service_new_constraint_names_match_migration():
    """The Epic 13 additions to existing models (custom_commands, workspace_services,
    workspaces) -- checked individually since these files also carry pre-existing names from
    earlier epics that this migration never touches."""
    migration_names = _names_in(MIGRATION_PATH)
    expected = {
        "ck_custom_commands_entry_point_kind",
        "ck_custom_commands_health_path",
        "uq_custom_commands_entry_point",
        "ck_workspace_services_health_status",
    }
    missing = expected - migration_names
    assert not missing, f"Expected Epic 13 names missing from migration: {missing}"

    command_path = Path(__file__).resolve().parent.parent / "app" / "models" / "command.py"
    command_names = _names_in(command_path)
    assert expected & command_names == {
        "ck_custom_commands_entry_point_kind",
        "ck_custom_commands_health_path",
        "uq_custom_commands_entry_point",
    }

    service_path = (
        Path(__file__).resolve().parent.parent / "app" / "models" / "workspace_service.py"
    )
    service_names = _names_in(service_path)
    assert "ck_workspace_services_health_status" in service_names


_PLATFORM_LLM_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0028_platform_llm_settings.py"
)


def test_platform_llm_settings_constraint_names_match_migration():
    model_path = Path(__file__).resolve().parent.parent / "app" / "models" / "platform_llm.py"
    model_names = _names_in(model_path)
    migration_names = _names_in(_PLATFORM_LLM_MIGRATION_PATH)
    missing = model_names - migration_names
    assert not missing, f"Names in model but not migration: {missing}"
