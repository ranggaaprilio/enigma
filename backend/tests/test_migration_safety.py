"""Expand/contract guard for Alembic migrations (Epic 08).

During a rolling deploy the migration Job runs first, then old and new API/worker replicas
serve side by side against the migrated schema. Every new migration must therefore be an
*expand* step the previous release keeps working against: add nullable (or server-defaulted)
columns, tables and indexes. Dropping, renaming, retyping or tightening a column -- or adding a
unique, foreign-key or check constraint -- breaks the old replicas still running, so it belongs
in a later *contract* release (after no deployed code depends on the old shape) and must say so
with a ``# contract: <why it is safe now>`` comment.

Pure AST check over ``upgrade()``, including literal ``op.execute`` SQL (non-literal SQL counts
as contract); ``downgrade()`` is expected to undo an expand.
"""

import ast
import re
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"
# Migrations up to here shipped before rolling deploys existed; they are not re-litigated.
BASELINE = 34
LEGACY_UNNUMBERED = {"40f4ecf72154_add_agent_file_changes"}
CONTRACT_MARKER = re.compile(r"^\s*#\s*contract:\s*\S", re.MULTILINE)

_ALWAYS_CONTRACT = {
    "drop_table",
    "drop_column",
    "rename_table",
    "drop_constraint",
    # New constraints reject writes the previous release still makes.
    "create_unique_constraint",
    "create_foreign_key",
    "create_check_constraint",
}
# Raw SQL that does what the ops above do. DROP INDEX is absent on purpose: it costs speed, not
# compatibility.
_CONTRACT_SQL = re.compile(
    r"\bDROP\s+(?:TABLE|COLUMN|CONSTRAINT|SCHEMA|TYPE)\b|\bRENAME\b|\bSET\s+NOT\s+NULL\b"
    r"|\bALTER\s+COLUMN\s+\S+\s+(?:SET\s+DATA\s+)?TYPE\b|\bTRUNCATE\b|\bADD\s+CONSTRAINT\b"
    r"|\bCREATE\s+UNIQUE\s+INDEX\b",
    re.IGNORECASE,
)


def _sql_literal(node: ast.expr) -> str | None:
    """The SQL string of ``op.execute("...")`` or ``op.execute(sa.text("..."))``, if literal."""
    if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "text" and node.args:
        node = node.args[0]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _kwargs(call: ast.Call) -> dict[str, ast.expr]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def _is_false(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _is_true(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _op_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id in {"op", "batch_op"}:
            return func.attr
    return None


def _column_breaks_old_writers(call: ast.Call) -> bool:
    """``add_column(..., sa.Column(..., nullable=False))`` without a ``server_default``."""
    for arg in call.args:
        if isinstance(arg, ast.Call) and getattr(arg.func, "attr", None) == "Column":
            kwargs = _kwargs(arg)
            if _is_false(kwargs.get("nullable")) and "server_default" not in kwargs:
                return True
    return False


def contract_operations(source: str) -> list[str]:
    """The backward-incompatible operations ``upgrade()`` performs, as ``op.<name>`` strings."""
    tree = ast.parse(source)
    upgrade = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None
    )
    if upgrade is None:
        return []
    found: list[str] = []
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call) or (name := _op_name(node)) is None:
            continue
        kwargs = _kwargs(node)
        if name in _ALWAYS_CONTRACT:
            found.append(f"op.{name}")
        elif name == "alter_column" and (
            "new_column_name" in kwargs or "type_" in kwargs or _is_false(kwargs.get("nullable"))
        ):
            found.append("op.alter_column")
        elif name == "add_column" and _column_breaks_old_writers(node):
            found.append("op.add_column(nullable=False, no server_default)")
        elif name == "create_index" and _is_true(kwargs.get("unique")):
            found.append("op.create_index(unique=True)")
        elif name == "execute" and node.args:
            sql = _sql_literal(node.args[0])
            # SQL built at runtime cannot be checked: treat it as contract until marked.
            if sql is None or _CONTRACT_SQL.search(sql):
                found.append("op.execute(contract SQL)")
    return found


def _gated(stem: str) -> bool:
    match = re.match(r"(\d{4})_", stem)
    if match:
        return int(match.group(1)) > BASELINE
    return stem not in LEGACY_UNNUMBERED


# --- the checker itself -------------------------------------------------------------------


def _migration(upgrade_body: str) -> str:
    return (
        "import sqlalchemy as sa\nfrom alembic import op\n\n\n"
        f"def upgrade() -> None:\n{upgrade_body}\n\n"
        "def downgrade() -> None:\n    op.drop_column('t', 'c')\n    op.drop_table('t')\n"
    )


@pytest.mark.parametrize(
    "body",
    [
        "    op.add_column('t', sa.Column('c', sa.Text(), nullable=True))",
        "    op.add_column('t', sa.Column('c', sa.Text()))",
        "    op.add_column('t', sa.Column('c', sa.Integer(), nullable=False, server_default='0'))",
        "    op.create_table('t', sa.Column('id', sa.Integer(), nullable=False))",
        "    op.create_index('ix_t_c', 't', ['c'])",
        "    op.alter_column('t', 'c', nullable=True)",
        "    op.alter_column('t', 'c', server_default='x')",
        # Dropping an index only costs speed; old code keeps working.
        "    op.drop_index('ix_t_c', table_name='t')",
        "    op.execute('UPDATE t SET c = 0 WHERE c IS NULL')",
        "    op.execute('CREATE INDEX CONCURRENTLY ix_t_c ON t (c)')",
    ],
)
def test_expand_operations_pass(body: str):
    assert contract_operations(_migration(body)) == []


@pytest.mark.parametrize(
    "body",
    [
        "    op.drop_column('t', 'c')",
        "    op.drop_table('t')",
        "    op.rename_table('t', 'u')",
        "    op.drop_constraint('uq_t_c', 't')",
        "    op.alter_column('t', 'c', new_column_name='d')",
        "    op.alter_column('t', 'c', type_=sa.BigInteger())",
        "    op.alter_column('t', 'c', nullable=False)",
        "    op.add_column('t', sa.Column('c', sa.Text(), nullable=False))",
        "    with op.batch_alter_table('t') as batch_op:\n        batch_op.drop_column('c')",
        # New constraints reject writes the previous release still makes.
        "    op.create_unique_constraint('uq_t_c', 't', ['c'])",
        "    op.create_foreign_key('fk_t_u', 't', 'u', ['u_id'], ['id'])",
        "    op.create_index('ix_t_c', 't', ['c'], unique=True)",
        "    op.create_check_constraint('ck_t_c', 't', 'c > 0')",
        # Raw SQL is not a way around the guard.
        "    op.execute('ALTER TABLE t DROP COLUMN c')",
        "    op.execute('ALTER TABLE t ALTER COLUMN c SET NOT NULL')",
        "    op.execute('alter table t rename column c to d')",
        "    op.execute('ALTER TABLE t ALTER COLUMN c TYPE bigint')",
        "    op.execute('DROP TABLE t')",
        "    op.execute(sa.text('TRUNCATE t'))",
    ],
)
def test_contract_operations_are_detected(body: str):
    assert contract_operations(_migration(body))


def test_downgrade_is_not_checked():
    assert contract_operations(_migration("    pass")) == []


def test_baseline_is_grandfathered():
    assert not _gated("0034_data_retention")
    assert not _gated("40f4ecf72154_add_agent_file_changes")
    assert _gated("0035_anything")
    assert _gated("deadbeef_unnumbered")


# --- the real migrations ------------------------------------------------------------------


def test_new_migrations_are_expand_only_or_marked_contract():
    offenders: list[str] = []
    for path in sorted(VERSIONS.glob("*.py")):
        if not _gated(path.stem):
            continue
        source = path.read_text()
        operations = contract_operations(source)
        if operations and not CONTRACT_MARKER.search(source):
            offenders.append(f"{path.name}: {', '.join(operations)}")
    assert not offenders, (
        "These migrations would break replicas still running the previous release during a "
        "rolling deploy. Split them into expand now / contract in a later release, and mark "
        "the contract step with '# contract: <why it is safe now>':\n  " + "\n  ".join(offenders)
    )
