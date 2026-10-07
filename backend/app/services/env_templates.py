"""Epic 13: ``${ENIGMA_*}`` template substitution in environment values.

Pure -- no DB, no sandbox -- mirroring ``run_detection.candidates_from_manifests``'s shape so
the interesting logic (addressing, error messages, escaping) is testable without a database.
The DB-touching half (``build_template_context``) lives in ``workspace_env.py``, which owns the
one seam (``prepare_launch_env``) that actually resolves templates -- deliberately NOT
``resolve()``, which `terminal.py` and `commands.py` also call and which has no way to surface
a hard-error path to two of its three callers.

Only ``ENIGMA_``-prefixed function names are ever touched: ``${DATABASE_URL}``, ``${FOO}``,
``$HOME`` all pass through byte-for-byte, so this is a closed namespace layered on top of
``env_file.py``'s "no shell-style interpolation" grammar, not a contradiction of it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

# A template is `${ENIGMA_NAME}` or `${ENIGMA_NAME:selector}`; `$${...}` is the escaped form,
# producing a literal `${...}` with no lookup. The escaped alternative is tried first so `$$`
# is only ever consumed when it immediately precedes `{ENIGMA_` -- a lone `$$` anywhere else
# (a password, a jq expression) is untouched.
_TEMPLATE = re.compile(
    r"\$\$\{(?P<esc_name>ENIGMA_[A-Z0-9_]+)(?::(?P<esc_arg>[^}]*))?\}"
    r"|\$\{(?P<name>ENIGMA_[A-Z0-9_]+)(?::(?P<arg>[^}]*))?\}"
)

# Functions that take a `:<selector>` argument and name a *sibling* service.
_SERVICE_FUNCTIONS = frozenset(
    {"ENIGMA_PREVIEW_URL", "ENIGMA_PREVIEW_HOST", "ENIGMA_SERVICE_PORT", "ENIGMA_SERVICE_URL"}
)
# Functions that take no argument.
_BARE_FUNCTIONS = frozenset({"ENIGMA_WORKSPACE_ID", "ENIGMA_BRANCH"})
_KNOWN_FUNCTIONS = _SERVICE_FUNCTIONS | _BARE_FUNCTIONS

# The one reserved selector: "this row", resolved from the *declared* port rather than the
# probed one, because the row launching its own env is by definition not running yet.
SELF_SELECTOR = "self"


class EnvPrepareError(Exception):
    """Common base for every hard-error path a launch-env preparation step can raise."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class EnvTemplateError(EnvPrepareError):
    def __init__(self, message: str, *, key: str, template: str, reason: str) -> None:
        super().__init__(message)
        self.key = key
        self.template = template
        self.reason = reason


def slugify_service_name(name: str) -> str:
    """NFKD-normalise, drop non-ASCII, lowercase, collapse any run of non-alphanumerics to a
    single hyphen, and strip leading/trailing hyphens. ``"Dev Server"`` -> ``"dev-server"``,
    ``"dev:api"`` -> ``"dev-api"``, ``"API "`` -> ``"api"``."""
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    collapsed = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower())
    return collapsed.strip("-")


@dataclass(frozen=True)
class ServiceRef:
    """One workspace_services row's addressable identity + current state, as seen by the
    template engine. Deliberately carries no ORM object -- just plain values -- so it stays
    testable without a session."""

    service_id: str
    custom_command_id: str | None
    name: str
    role: str
    status: str
    # The currently-probed port (None if not running) -- used for a *sibling* reference.
    running_port: int | None
    # `command.port or infer_port(command.command)` -- used for a *self* reference, since the
    # row launching its own env is, by definition, not running yet.
    declared_port: int | None
    is_builtin: bool
    # The stable alias URL, if aliases are enabled for the workspace and one has been minted
    # for this row; otherwise None.
    alias_url: str | None = None


@dataclass(frozen=True)
class TemplateContext:
    workspace_id: str
    branch: str
    aliases_enabled: bool
    services: tuple[ServiceRef, ...] = ()
    # Which `services` entry (by `service_id`) is launching right now, or None for a caller
    # with no single "self" row (e.g. a terminal, which serves the whole workspace).
    self_service_id: str | None = None


@dataclass(frozen=True)
class TemplateProblem:
    key: str
    template: str
    selector: str | None
    # "unknown_service" | "ambiguous_service" | "not_running" | "aliases_disabled" |
    # "unknown_function"
    reason: str
    message: str


@dataclass(frozen=True)
class TemplateReference:
    """A bare reference found by `references()` -- key/template/selector only, never a value,
    matching the discipline `workspace_env.egress_warnings` already documents."""

    key: str
    template: str
    selector: str | None


def _candidate_aliases(ref: ServiceRef) -> set[str]:
    candidates: set[str] = set()
    slug = slugify_service_name(ref.name)
    if slug:
        candidates.add(slug)
    tail = re.split(r"[:\-_]", ref.name)[-1]
    tail_slug = slugify_service_name(tail)
    if tail_slug:
        candidates.add(tail_slug)
    if ref.role and ref.role != "custom":
        candidates.add(ref.role)
    candidates.add(ref.service_id)  # the unambiguous escape hatch
    if ref.is_builtin:
        candidates.add("dev-server")
        candidates.add("dev")
    return candidates


def build_alias_index(services: tuple[ServiceRef, ...]) -> dict[str, ServiceRef | None]:
    """Maps a slugified selector to the one row it names, or `None` when two rows both claim
    it. An ambiguous alias is *removed* from the index rather than resolved by precedence --
    precedence decides which aliases exist, never which row silently wins a naming collision."""
    index: dict[str, ServiceRef | None] = {}
    for ref in services:
        for candidate in _candidate_aliases(ref):
            index[candidate] = None if candidate in index else ref
    return index


def _known_service_names(context: TemplateContext) -> str:
    if not context.services:
        return "(no services configured)"
    return ", ".join(ref.name for ref in context.services)


def _resolve_selector(
    selector: str,
    context: TemplateContext,
    alias_index: dict[str, ServiceRef | None],
) -> tuple[ServiceRef | None, str | None]:
    """Returns `(ref, error_reason)`. `error_reason` is `None` on success, else
    "unknown_service" or "ambiguous_service"."""
    if selector == SELF_SELECTOR:
        if context.self_service_id is None:
            return None, "unknown_service"
        for ref in context.services:
            if ref.service_id == context.self_service_id:
                return ref, None
        return None, "unknown_service"
    key = slugify_service_name(selector)
    if key not in alias_index:
        return None, "unknown_service"
    ref = alias_index[key]
    if ref is None:
        return None, "ambiguous_service"
    return ref, None


def _ambiguous_names(selector: str, context: TemplateContext) -> str:
    key = slugify_service_name(selector)
    matches = [ref.name for ref in context.services if key in _candidate_aliases(ref)]
    return ", ".join(matches)


def _port_for(ref: ServiceRef, context: TemplateContext) -> int | None:
    if ref.service_id == context.self_service_id:
        return ref.declared_port
    return ref.running_port


def _resolve_function(
    name: str,
    arg: str | None,
    context: TemplateContext,
    alias_index: dict[str, ServiceRef | None],
    *,
    key: str,
    template: str,
) -> tuple[str, TemplateProblem | None]:
    if name == "ENIGMA_WORKSPACE_ID":
        return context.workspace_id, None
    if name == "ENIGMA_BRANCH":
        return context.branch, None

    if name not in _KNOWN_FUNCTIONS:
        return template, TemplateProblem(
            key=key,
            template=template,
            selector=arg,
            reason="unknown_function",
            message=f"{key}: {template} is not a recognised Enigma template function.",
        )

    selector = arg or ""
    ref, error_reason = _resolve_selector(selector, context, alias_index)
    if error_reason == "unknown_service":
        return template, TemplateProblem(
            key=key,
            template=template,
            selector=selector,
            reason="unknown_service",
            message=(
                f"{key}: {template} -- no service named {selector!r}. "
                f"Known services: {_known_service_names(context)}."
            ),
        )
    if error_reason == "ambiguous_service":
        return template, TemplateProblem(
            key=key,
            template=template,
            selector=selector,
            reason="ambiguous_service",
            message=(
                f"{key}: {template} -- {selector!r} matches more than one service "
                f"({_ambiguous_names(selector, context)}); use the full name or the service id."
            ),
        )
    assert ref is not None

    if name in ("ENIGMA_PREVIEW_URL", "ENIGMA_PREVIEW_HOST"):
        if not context.aliases_enabled or ref.alias_url is None:
            return template, TemplateProblem(
                key=key,
                template=template,
                selector=selector,
                reason="aliases_disabled",
                message=(
                    f"{key}: {template} needs a stable URL. Enable stable preview aliases for "
                    f"this workspace, or use ${{ENIGMA_SERVICE_URL:{selector}}} for "
                    "server-side calls."
                ),
            )
        if name == "ENIGMA_PREVIEW_HOST":
            return urlsplit(ref.alias_url).netloc, None
        return ref.alias_url, None

    # ENIGMA_SERVICE_PORT / ENIGMA_SERVICE_URL
    port = _port_for(ref, context)
    if port is None:
        return template, TemplateProblem(
            key=key,
            template=template,
            selector=selector,
            reason="not_running",
            message=(
                f"{key}: {template} -- service {ref.name!r} is not running "
                f"({ref.status}). Start it first."
            ),
        )
    if name == "ENIGMA_SERVICE_PORT":
        return str(port), None
    return f"http://127.0.0.1:{port}", None


def _substitute_value(
    key: str,
    value: str,
    context: TemplateContext,
    alias_index: dict[str, ServiceRef | None],
) -> tuple[str, list[TemplateProblem]]:
    problems: list[TemplateProblem] = []

    def replace(match: re.Match[str]) -> str:
        if match.group("esc_name") is not None:
            name = match.group("esc_name")
            arg = match.group("esc_arg")
            suffix = f":{arg}" if arg is not None else ""
            return f"${{{name}{suffix}}}"
        name = match.group("name")
        arg = match.group("arg")
        template_text = match.group(0)
        replacement, problem = _resolve_function(
            name, arg, context, alias_index, key=key, template=template_text
        )
        if problem is not None:
            problems.append(problem)
            return template_text
        return replacement

    return _TEMPLATE.sub(replace, value), problems


def substitute(
    values: dict[str, str], context: TemplateContext, *, strict: bool
) -> tuple[dict[str, str], list[TemplateProblem]]:
    """Resolves every ``${ENIGMA_*}`` template in `values`.

    In `strict` mode (every launch site, and command runs), the *first* unresolvable template
    raises `EnvTemplateError` immediately -- a resolution failure must never become a silently
    empty string. In non-strict mode (a terminal only -- the one place you go to *debug* a bad
    template), an offending key is *omitted* from the result (never emptied) and every problem
    is returned for the caller to surface as a notice.
    """
    alias_index = build_alias_index(context.services)
    resolved: dict[str, str] = {}
    problems: list[TemplateProblem] = []

    for key, value in values.items():
        new_value, key_problems = _substitute_value(key, value, context, alias_index)
        if key_problems:
            problems.extend(key_problems)
            if strict:
                first = key_problems[0]
                raise EnvTemplateError(
                    first.message, key=first.key, template=first.template, reason=first.reason
                )
            continue  # non-strict: omit this key entirely, never an emptied placeholder
        resolved[key] = new_value

    return resolved, problems


def enigma_exports(context: TemplateContext) -> dict[str, str]:
    """Values merged into the env *before* substitution (so a user's own key of the same name
    always wins -- the caller is responsible for that merge order)."""
    exports: dict[str, str] = {
        "ENIGMA_WORKSPACE_ID": context.workspace_id,
        "ENIGMA_BRANCH": context.branch,
    }
    self_ref = next(
        (ref for ref in context.services if ref.service_id == context.self_service_id), None
    )
    if self_ref is not None:
        exports["ENIGMA_SERVICE_NAME"] = self_ref.name
        if self_ref.declared_port is not None:
            exports["ENIGMA_SERVICE_PORT"] = str(self_ref.declared_port)

    for ref in context.services:
        if ref.service_id == context.self_service_id or ref.running_port is None:
            continue
        slug = slugify_service_name(ref.name).upper().replace("-", "_")
        if not slug:
            continue
        exports[f"ENIGMA_PORT_{slug}"] = str(ref.running_port)
        exports[f"ENIGMA_URL_{slug}"] = f"http://127.0.0.1:{ref.running_port}"
    return exports


def references(values: dict[str, str]) -> list[TemplateReference]:
    """Every service-addressing template reference in `values` -- key/template/selector only,
    never the surrounding value -- for the write-time "references a service that does not
    exist" warning and the composition endpoint's `template_unresolved` advisory."""
    found: list[TemplateReference] = []
    for key, value in values.items():
        for match in _TEMPLATE.finditer(value):
            if match.group("esc_name") is not None:
                continue
            name = match.group("name")
            if name in _SERVICE_FUNCTIONS:
                found.append(
                    TemplateReference(key=key, template=match.group(0), selector=match.group("arg"))
                )
    return found


def strip_templates(value: str) -> str:
    """`value` with every (non-escaped) `${ENIGMA_*}` template removed -- used to detect a
    value that is *only* template text, so it is never over-redacted (see
    `workspace_env.redactable_env_values`)."""
    return _TEMPLATE.sub("", value)
