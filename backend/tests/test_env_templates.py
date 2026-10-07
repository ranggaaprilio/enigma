"""Epic 13: the ``${ENIGMA_*}`` template engine -- pure, no DB, no sandbox."""

import pytest

from app.services.env_templates import (
    EnvTemplateError,
    ServiceRef,
    TemplateContext,
    build_alias_index,
    enigma_exports,
    references,
    slugify_service_name,
    strip_templates,
    substitute,
)


def _ref(
    *,
    service_id="s1",
    custom_command_id=None,
    name="API",
    role="dev",
    status="running",
    running_port=8000,
    declared_port=8000,
    is_builtin=False,
    alias_url=None,
) -> ServiceRef:
    return ServiceRef(
        service_id=service_id,
        custom_command_id=custom_command_id,
        name=name,
        role=role,
        status=status,
        running_port=running_port,
        declared_port=declared_port,
        is_builtin=is_builtin,
        alias_url=alias_url,
    )


def _context(services=(), *, aliases_enabled=True, self_service_id=None, branch="main"):
    return TemplateContext(
        workspace_id="workspace-1",
        branch=branch,
        aliases_enabled=aliases_enabled,
        services=tuple(services),
        self_service_id=self_service_id,
    )


# ---------------------------------------------------------------------------
# slugify_service_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Dev Server", "dev-server"),
        ("dev:api", "dev-api"),
        ("API ", "api"),
        ("  Multi   Space  ", "multi-space"),
        ("", ""),
        ("---", ""),
    ],
)
def test_slugify_service_name(name, expected):
    assert slugify_service_name(name) == expected


# ---------------------------------------------------------------------------
# build_alias_index
# ---------------------------------------------------------------------------


def test_alias_index_includes_name_tail_role_and_id():
    ref = _ref(service_id="abc123", name="dev:api", role="dev")
    index = build_alias_index((ref,))
    assert index["dev-api"] is ref  # full slugified name
    assert index["api"] is ref  # tail after ':'
    assert index["dev"] is ref  # role
    assert index["abc123"] is ref  # id escape hatch


def test_builtin_row_also_answers_dev_server_and_dev():
    ref = _ref(service_id="builtin", name="Dev Server", role="dev", is_builtin=True)
    index = build_alias_index((ref,))
    assert index["dev-server"] is ref
    assert index["dev"] is ref


def test_a_collision_removes_the_alias_rather_than_picking_a_winner():
    ref_a = _ref(service_id="a", name="api", role="custom")
    ref_b = _ref(service_id="b", name="api", role="custom")
    index = build_alias_index((ref_a, ref_b))
    assert index["api"] is None  # ambiguous -- neither wins
    assert index["a"] is ref_a  # the id escape hatch is still unique
    assert index["b"] is ref_b


# ---------------------------------------------------------------------------
# substitute: happy paths
# ---------------------------------------------------------------------------


def test_preview_url_resolves_to_the_alias_url():
    ref = _ref(alias_url="https://demo-api-a1b2c3.preview.example.com/")
    context = _context([ref])
    resolved, problems = substitute(
        {"API_BASE_URL": "${ENIGMA_PREVIEW_URL:API}"}, context, strict=True
    )
    assert problems == []
    assert resolved["API_BASE_URL"] == "https://demo-api-a1b2c3.preview.example.com/"


def test_preview_host_resolves_to_just_the_netloc():
    ref = _ref(alias_url="https://demo-api-a1b2c3.preview.example.com/")
    context = _context([ref])
    resolved, _ = substitute({"H": "${ENIGMA_PREVIEW_HOST:API}"}, context, strict=True)
    assert resolved["H"] == "demo-api-a1b2c3.preview.example.com"


def test_service_port_resolves_to_the_running_port_for_a_sibling():
    ref = _ref(running_port=8000)
    context = _context([ref])
    resolved, _ = substitute({"P": "${ENIGMA_SERVICE_PORT:API}"}, context, strict=True)
    assert resolved["P"] == "8000"


def test_service_url_resolves_to_a_loopback_url():
    ref = _ref(running_port=8000)
    context = _context([ref])
    resolved, _ = substitute({"U": "${ENIGMA_SERVICE_URL:API}"}, context, strict=True)
    assert resolved["U"] == "http://127.0.0.1:8000"


def test_workspace_id_and_branch_need_no_selector():
    context = _context([], branch="feature/x")
    resolved, _ = substitute(
        {"W": "${ENIGMA_WORKSPACE_ID}", "B": "${ENIGMA_BRANCH}"}, context, strict=True
    )
    assert resolved["W"] == "workspace-1"
    assert resolved["B"] == "feature/x"


def test_self_resolves_from_the_declared_port_not_the_running_port():
    """The row launching its own env is not running yet -- self must use the declared port."""
    ref = _ref(service_id="self-id", running_port=None, declared_port=9000)
    context = _context([ref], self_service_id="self-id")
    resolved, problems = substitute({"P": "${ENIGMA_SERVICE_PORT:self}"}, context, strict=True)
    assert problems == []
    assert resolved["P"] == "9000"


def test_self_by_name_also_uses_declared_port():
    """Even addressing the row by its own name/alias (not literally 'self') must use the
    declared port when that row happens to be the one currently launching."""
    ref = _ref(service_id="self-id", name="api", running_port=None, declared_port=9000)
    context = _context([ref], self_service_id="self-id")
    resolved, problems = substitute({"P": "${ENIGMA_SERVICE_PORT:api}"}, context, strict=True)
    assert problems == []
    assert resolved["P"] == "9000"


# ---------------------------------------------------------------------------
# Escaping and pass-through
# ---------------------------------------------------------------------------


def test_escaped_template_becomes_a_literal():
    context = _context([])
    resolved, problems = substitute(
        {"X": "$${ENIGMA_WORKSPACE_ID}"}, context, strict=True
    )
    assert problems == []
    assert resolved["X"] == "${ENIGMA_WORKSPACE_ID}"


def test_a_lone_double_dollar_elsewhere_is_untouched():
    context = _context([])
    resolved, _ = substitute({"PW": "p$$assword"}, context, strict=True)
    assert resolved["PW"] == "p$$assword"


def test_non_enigma_templates_pass_through_byte_for_byte():
    context = _context([])
    resolved, problems = substitute(
        {"X": "${DATABASE_URL} and $HOME and ${FOO:bar}"}, context, strict=True
    )
    assert problems == []
    assert resolved["X"] == "${DATABASE_URL} and $HOME and ${FOO:bar}"


# ---------------------------------------------------------------------------
# Failure reasons -- each names the fix, never the value
# ---------------------------------------------------------------------------


def test_unknown_service_lists_known_names_and_never_the_value():
    ref = _ref(name="Web")
    context = _context([ref])
    with pytest.raises(EnvTemplateError) as exc_info:
        substitute({"SECRET_X": "${ENIGMA_PREVIEW_URL:api}"}, context, strict=True)
    error = exc_info.value
    assert error.reason == "unknown_service"
    assert "api" in error.message
    assert "Web" in error.message
    assert "SECRET_X" not in error.message.split(":")[1]  # value never echoed, only the key name


def test_ambiguous_service_names_the_candidates():
    ref_a = _ref(service_id="a", name="api-worker")
    ref_b = _ref(service_id="b", name="api-server")
    context = _context([ref_a, ref_b])
    with pytest.raises(EnvTemplateError) as exc_info:
        substitute({"X": "${ENIGMA_SERVICE_PORT:api}"}, context, strict=True)
    assert exc_info.value.reason == "unknown_service"  # neither name slugifies to bare "api"

    ref_c = _ref(service_id="c", name="api", role="custom")
    ref_d = _ref(service_id="d", name="api", role="custom")
    context2 = _context([ref_c, ref_d])
    with pytest.raises(EnvTemplateError) as exc_info2:
        substitute({"X": "${ENIGMA_SERVICE_PORT:api}"}, context2, strict=True)
    assert exc_info2.value.reason == "ambiguous_service"
    assert "api" in exc_info2.value.message


def test_not_running_names_the_service_and_its_status():
    ref = _ref(name="API", status="stopped", running_port=None)
    context = _context([ref])
    with pytest.raises(EnvTemplateError) as exc_info:
        substitute({"X": "${ENIGMA_SERVICE_PORT:API}"}, context, strict=True)
    error = exc_info.value
    assert error.reason == "not_running"
    assert "API" in error.message
    assert "stopped" in error.message


def test_aliases_disabled_names_the_fix_and_suggests_service_url():
    ref = _ref(alias_url=None)
    context = _context([ref], aliases_enabled=False)
    with pytest.raises(EnvTemplateError) as exc_info:
        substitute({"X": "${ENIGMA_PREVIEW_URL:API}"}, context, strict=True)
    error = exc_info.value
    assert error.reason == "aliases_disabled"
    assert "enable stable preview aliases" in error.message.lower()
    assert "ENIGMA_SERVICE_URL" in error.message


def test_unknown_function_is_a_hard_error_never_a_pass_through():
    context = _context([])
    with pytest.raises(EnvTemplateError) as exc_info:
        substitute({"X": "${ENIGMA_PREVIW_URL:api}"}, context, strict=True)
    assert exc_info.value.reason == "unknown_function"


def test_error_message_never_contains_the_original_value_text():
    """The failure reasons must name the key/template/reason, never leak the *other* half of
    the value dict or any secret that happened to sit in a different key."""
    ref = _ref(name="Web")
    context = _context([ref])
    with pytest.raises(EnvTemplateError) as exc_info:
        substitute(
            {"X": "${ENIGMA_PREVIEW_URL:nonexistent}"}, context, strict=True
        )
    assert "nonexistent" in exc_info.value.message
    # The message is scoped to this one key/template -- no unrelated secret material leaks in.


# ---------------------------------------------------------------------------
# Non-strict mode (the terminal exception)
# ---------------------------------------------------------------------------


def test_non_strict_mode_omits_the_offending_key_and_reports_the_problem():
    context = _context([])
    resolved, problems = substitute(
        {"GOOD": "plain-value", "BAD": "${ENIGMA_PREVIEW_URL:nope}"}, context, strict=False
    )
    assert "GOOD" in resolved
    assert "BAD" not in resolved  # omitted, never emptied to ""
    assert len(problems) == 1
    assert problems[0].reason == "unknown_service"


def test_non_strict_mode_never_raises():
    context = _context([])
    resolved, problems = substitute(
        {"X": "${ENIGMA_PREVIW_URL:x}"}, context, strict=False
    )
    assert "X" not in resolved
    assert problems[0].reason == "unknown_function"


# ---------------------------------------------------------------------------
# enigma_exports
# ---------------------------------------------------------------------------


def test_enigma_exports_carries_workspace_and_branch():
    context = _context([], branch="feature/y")
    exports = enigma_exports(context)
    assert exports["ENIGMA_WORKSPACE_ID"] == "workspace-1"
    assert exports["ENIGMA_BRANCH"] == "feature/y"


def test_enigma_exports_names_the_self_row():
    ref = _ref(service_id="self-id", name="API", declared_port=8000)
    context = _context([ref], self_service_id="self-id")
    exports = enigma_exports(context)
    assert exports["ENIGMA_SERVICE_NAME"] == "API"
    assert exports["ENIGMA_SERVICE_PORT"] == "8000"


def test_enigma_exports_names_each_running_sibling_port_and_url():
    sibling = _ref(service_id="sib", name="dev:api", running_port=8000)
    context = _context([sibling], self_service_id="self-id")
    exports = enigma_exports(context)
    assert exports["ENIGMA_PORT_DEV_API"] == "8000"
    assert exports["ENIGMA_URL_DEV_API"] == "http://127.0.0.1:8000"


def test_enigma_exports_skips_a_sibling_with_no_running_port():
    sibling = _ref(service_id="sib", name="idle", running_port=None)
    context = _context([sibling])
    exports = enigma_exports(context)
    assert "ENIGMA_PORT_IDLE" not in exports


def test_user_key_shadowing_an_export_wins_when_merged_by_the_caller():
    """`enigma_exports` itself has no notion of precedence -- the caller merges
    `{**enigma_exports(context), **user_env}` so a user key always overrides. This test pins
    the merge order contract at the call site, not inside this pure function."""
    context = _context([], branch="main")
    exports = enigma_exports(context)
    merged = {**exports, "ENIGMA_BRANCH": "user-override"}
    assert merged["ENIGMA_BRANCH"] == "user-override"


# ---------------------------------------------------------------------------
# references() -- for the write-time warning and the composition advisory
# ---------------------------------------------------------------------------


def test_references_returns_key_template_selector_only():
    refs = references({"API_BASE_URL": "prefix ${ENIGMA_PREVIEW_URL:api} suffix"})
    assert len(refs) == 1
    assert refs[0].key == "API_BASE_URL"
    assert refs[0].template == "${ENIGMA_PREVIEW_URL:api}"
    assert refs[0].selector == "api"


def test_references_ignores_escaped_and_non_service_templates():
    refs = references(
        {
            "A": "$${ENIGMA_PREVIEW_URL:api}",  # escaped
            "B": "${ENIGMA_WORKSPACE_ID}",  # not a service-addressing function
        }
    )
    assert refs == []


# ---------------------------------------------------------------------------
# strip_templates -- for the redaction fix
# ---------------------------------------------------------------------------


def test_strip_templates_removes_template_text():
    assert strip_templates("${ENIGMA_PREVIEW_URL:api}") == ""


def test_a_template_only_value_is_excluded_from_redaction_by_being_empty_after_stripping():
    from app.services.workspace_env import redactable_env_values

    env = {"API_BASE_URL": "${ENIGMA_PREVIEW_URL:api}"}
    assert redactable_env_values(env) == ()


def test_a_value_with_real_content_around_a_template_is_not_considered_template_only():
    assert strip_templates("prefix-${ENIGMA_WORKSPACE_ID}-suffix") == "prefix--suffix"
