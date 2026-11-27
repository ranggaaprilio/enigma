"""Plan 4 Task 2: the body of the PR an approved Spec Kit workflow is handed over as."""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.services.spec_pull_request import MAX_BODY_CHARS, SPEC_EXCERPT_CHARS, build_pr_body

SECRET_OUTPUT = "AKIA-SECRET-IN-TEST-OUTPUT"


def _workflow(**overrides):
    base = dict(
        feature_name="Export CSV",
        feature_slug="001-export-csv",
        description="Users can download orders as CSV.",
        branch="001-export-csv",
        spec_dir="specs/001-export-csv",
    )
    return SimpleNamespace(**{**base, **overrides})


def _run(*evidence, iteration=1, command="pytest -q"):
    return SimpleNamespace(
        criteria={"test_command": command},
        iteration=iteration,
        evidence=[
            SimpleNamespace(kind=kind, status=status, detail=detail, created_at=at)
            for kind, status, detail, at in evidence
        ],
    )


T0 = datetime(2026, 11, 27, 10, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 27, 10, 5, tzinfo=UTC)


def test_body_links_the_spec_kit_artifacts_on_the_branch() -> None:
    body = build_pr_body(
        _workflow(),
        repo_url="https://github.com/acme/shop.git",
        spec_excerpt="# Spec\nOrders export.",
        run=None,
        approved_at=None,
    )
    base = "https://github.com/acme/shop/blob/001-export-csv/specs/001-export-csv"
    for name in ("spec.md", "plan.md", "tasks.md"):
        assert f"{base}/{name}" in body
    assert "Users can download orders as CSV." in body
    assert "Orders export." in body


def test_body_summarises_the_approved_evidence_without_its_output() -> None:
    run = _run(
        ("test", "failed", {"exit_code": 1, "output": SECRET_OUTPUT}, T0),
        ("fix", "passed", {"agent_run_id": "x"}, T0),
        ("test", "passed", {"exit_code": 0, "output": SECRET_OUTPUT}, T1),
        ("observation", "passed", {"summary": f"Served at http://10.0.0.2 {SECRET_OUTPUT}"}, T1),
        iteration=1,
    )
    body = build_pr_body(
        _workflow(),
        repo_url="https://github.com/acme/shop",
        spec_excerpt=None,
        run=run,
        approved_at=T1,
    )
    assert "`pytest -q`" in body
    assert "| Last test | passed |" in body
    assert "| Fix attempts | 1 |" in body
    assert "| Observation | passed |" in body
    assert "2026-11-27" in body
    assert SECRET_OUTPUT not in body
    assert "10.0.0.2" not in body


def test_body_says_when_a_workflow_was_not_verified() -> None:
    body = build_pr_body(
        _workflow(),
        repo_url="https://github.com/acme/shop",
        spec_excerpt=None,
        run=None,
        approved_at=None,
    )
    assert "Not verified" in body


def test_body_truncates_the_excerpt_and_caps_its_length() -> None:
    long_spec = "x" * (SPEC_EXCERPT_CHARS + 500)
    body = build_pr_body(
        _workflow(description="d" * (MAX_BODY_CHARS * 2)),
        repo_url="https://github.com/acme/shop",
        spec_excerpt=long_spec,
        run=None,
        approved_at=None,
    )
    assert "x" * (SPEC_EXCERPT_CHARS + 1) not in body
    assert len(body) <= MAX_BODY_CHARS


def test_body_keeps_the_test_command_on_one_safe_line() -> None:
    run = _run(("test", "passed", {}, T0), iteration=0, command="pytest `x`\nmore")
    body = build_pr_body(
        _workflow(),
        repo_url="https://github.com/acme/shop",
        spec_excerpt=None,
        run=run,
        approved_at=T0,
    )
    line = next(line for line in body.splitlines() if line.startswith("| Test command"))
    assert "more" in line
    assert line.count("``") == 2  # wrapped in a double-backtick code span


def test_spec_excerpt_is_fenced_so_mentions_do_not_notify() -> None:
    """Security review: the excerpt is agent-written; in a code block `@team`/`#1` stay inert."""
    body = build_pr_body(
        _workflow(),
        repo_url="https://github.com/acme/shop",
        spec_excerpt="ping @acme/everyone about #1\n```\nnested fence\n```",
        run=None,
        approved_at=None,
    )
    excerpt_start = body.index("````markdown")
    assert excerpt_start < body.index("@acme/everyone") < body.index("\n````\n", excerpt_start)


def _run_read(tmp_path, rel_path):
    import subprocess

    from app.services.spec_pull_request import confined_read_command

    return subprocess.run(
        ["sh", "-c", confined_read_command(rel_path, 10)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def test_confined_read_reads_a_regular_file_up_to_the_limit(tmp_path) -> None:
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "spec.md").write_text("0123456789ABCDEF")
    result = _run_read(tmp_path, "specs/spec.md")
    assert result.returncode == 0
    assert result.stdout == "0123456789"


def test_confined_read_refuses_symlinks_out_of_the_worktree(tmp_path) -> None:
    """Security review: a symlinked spec.md (or spec dir) must not leak files into the PR."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("TOKEN")
    tree = tmp_path / "tree"
    (tree / "specs").mkdir(parents=True)
    (tree / "specs" / "spec.md").symlink_to(outside / "secret")
    (tree / "linkdir").symlink_to(outside)

    for rel in ("specs/spec.md", "linkdir/secret", "../outside/secret", "missing.md"):
        result = _run_read(tree, rel)
        assert result.returncode != 0, rel
        assert "TOKEN" not in result.stdout, rel
