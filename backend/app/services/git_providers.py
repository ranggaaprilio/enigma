"""Git hosting provider abstraction.

Everything in here is pure (no I/O, no DB) so it can be unit-tested exhaustively and reused by
the repository service, the pull-request service, and the API layer. It answers three questions:

1. *Which provider is this URL for?* (``detect_provider`` / ``parse_repository_url``)
2. *What does a canonical, credential-free URL look like?* (``sanitize_repository_url``)
3. *How does HTTP basic auth / redaction work for that provider?* (``http_basic_username``,
   ``redact_secrets``)

URL validation is deliberately strict: only ``https://`` and SSH (``ssh://git@host/...`` or the
scp-like ``git@host:...``) forms are accepted, userinfo is always stripped, and hosts that could
point back at the platform itself (IP literals, ``localhost``, internal-only suffixes, dotless
names) are rejected to keep the clone target from being used for SSRF-style probing. Validation is
literal-only — no DNS — because the clone runs inside the sandbox whose egress is governed
separately by the Daytona network allow-list.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

PROVIDER_SLUGS: tuple[str, ...] = ("github", "gitlab", "bitbucket", "generic")
AUTH_TYPES: tuple[str, ...] = ("personal_access_token", "ssh_key")
AUTO_PROVIDER = "auto"

DEFAULT_URL_ERROR = "Repository URL must be an HTTPS or SSH git URL"

_SCP_LIKE = re.compile(r"^(?P<user>[A-Za-z0-9_.-]+)@(?P<host>[^:/\s@]+):(?P<path>[^\s]+)$")
_HOST_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_USERINFO = re.compile(r"(https?|ssh)://[^@\s/]+@")

# Hosts (or suffixes) that can never be a public git host and would only be useful to probe the
# platform's own network from the sandbox.
_BLOCKED_HOSTS = frozenset({"localhost", "localhost.localdomain"})
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".localdomain", ".home.arpa")


class InvalidRepositoryUrlError(Exception):
    def __init__(self, message: str = DEFAULT_URL_ERROR) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class GitProviderSpec:
    slug: str
    display_name: str
    # Hosts that identify this provider during auto-detection. Empty for ``generic``.
    hosts: frozenset[str]
    # The host written into the canonical URL when one of ``hosts`` matched (``www.`` collapse).
    canonical_host: str | None
    # Username half of HTTP basic auth when authenticating with a token.
    http_basic_username: str
    supports_pull_requests: bool
    min_path_segments: int
    max_path_segments: int | None


PROVIDERS: dict[str, GitProviderSpec] = {
    "github": GitProviderSpec(
        slug="github",
        display_name="GitHub",
        hosts=frozenset({"github.com", "www.github.com"}),
        canonical_host="github.com",
        http_basic_username="x-access-token",
        supports_pull_requests=True,
        min_path_segments=2,
        max_path_segments=2,
    ),
    "gitlab": GitProviderSpec(
        slug="gitlab",
        display_name="GitLab",
        hosts=frozenset({"gitlab.com", "www.gitlab.com"}),
        canonical_host="gitlab.com",
        http_basic_username="oauth2",
        supports_pull_requests=False,
        min_path_segments=2,
        # GitLab supports nested groups: group/subgroup/project.
        max_path_segments=None,
    ),
    "bitbucket": GitProviderSpec(
        slug="bitbucket",
        display_name="Bitbucket",
        hosts=frozenset({"bitbucket.org", "www.bitbucket.org"}),
        canonical_host="bitbucket.org",
        http_basic_username="x-token-auth",
        supports_pull_requests=False,
        min_path_segments=2,
        max_path_segments=2,
    ),
    "generic": GitProviderSpec(
        slug="generic",
        display_name="Git host",
        hosts=frozenset(),
        canonical_host=None,
        http_basic_username="git",
        supports_pull_requests=False,
        min_path_segments=1,
        max_path_segments=None,
    ),
}

# Providers whose hosts are fixed SaaS endpoints — an explicit choice must match the host.
_HOST_BOUND_PROVIDERS = frozenset({"github", "bitbucket"})


@dataclass(frozen=True)
class ParsedRepositoryUrl:
    provider: str
    scheme: Literal["https", "ssh"]
    host: str
    port: int | None
    # ``owner/repo.git`` — no leading slash, always ends with ``.git``.
    path: str
    # Canonical, credential-free URL: ``https://host[:port]/owner/repo.git`` or
    # ``ssh://git@host[:port]/owner/repo.git``.
    url: str


def provider_spec(provider: str) -> GitProviderSpec:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise InvalidRepositoryUrlError(f"Unknown git provider '{provider}'") from None


def provider_for_host(host: str) -> str:
    lowered = host.lower()
    for spec in PROVIDERS.values():
        if lowered in spec.hosts:
            return spec.slug
    return "generic"


def supports_pull_requests(provider: str | None) -> bool:
    if provider is None:
        return False
    spec = PROVIDERS.get(provider)
    return bool(spec and spec.supports_pull_requests)


def http_basic_username(provider: str, override: str | None = None) -> str:
    """Username half of the HTTP basic header for token auth.

    GitHub ignores the username, GitLab requires ``oauth2`` for project/personal access tokens,
    Bitbucket Cloud uses ``x-token-auth`` for repository/workspace access tokens but the real
    account username for *app passwords* — hence the optional override.
    """
    if override and override.strip():
        return override.strip()
    return provider_spec(provider).http_basic_username


def _validate_host(host: str) -> str:
    lowered = host.strip().lower().rstrip(".")
    if not lowered or len(lowered) > 253:
        raise InvalidRepositoryUrlError("Repository host is invalid")
    try:
        ipaddress.ip_address(lowered)
    except ValueError:
        pass
    else:
        raise InvalidRepositoryUrlError("Repository host must be a hostname, not an IP address")
    if lowered in _BLOCKED_HOSTS or lowered.endswith(_BLOCKED_SUFFIXES):
        raise InvalidRepositoryUrlError("Repository host is not allowed")
    labels = lowered.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise InvalidRepositoryUrlError("Repository host is invalid")
    return lowered


def _normalize_path(raw_path: str, spec: GitProviderSpec) -> str:
    path = raw_path.strip().strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.rstrip("/")
    if not path:
        raise InvalidRepositoryUrlError("Repository URL is missing the repository path")
    segments = path.split("/")
    for segment in segments:
        if segment in {".", ".."} or not _PATH_SEGMENT.fullmatch(segment):
            raise InvalidRepositoryUrlError("Repository path contains invalid characters")
    if len(segments) < spec.min_path_segments:
        raise InvalidRepositoryUrlError(
            f"{spec.display_name} repository URLs must look like owner/repository"
        )
    if spec.max_path_segments is not None and len(segments) > spec.max_path_segments:
        raise InvalidRepositoryUrlError(
            f"{spec.display_name} repository URLs must look like owner/repository"
        )
    return "/".join(segments) + ".git"


def _split_url(repo_url: str) -> tuple[Literal["https", "ssh"], str, int | None, str]:
    """Return ``(scheme, host, port, raw_path)`` or raise."""
    raw = repo_url.strip()
    if not raw:
        raise InvalidRepositoryUrlError()
    if "://" not in raw:
        match = _SCP_LIKE.match(raw)
        if match is None:
            raise InvalidRepositoryUrlError()
        if match.group("user") != "git":
            raise InvalidRepositoryUrlError("SSH repository URLs must use the 'git' user")
        return "ssh", match.group("host"), None, match.group("path")
    parsed = urlparse(raw)
    if parsed.scheme == "https":
        scheme: Literal["https", "ssh"] = "https"
    elif parsed.scheme == "ssh":
        scheme = "ssh"
        if parsed.username not in (None, "git"):
            raise InvalidRepositoryUrlError("SSH repository URLs must use the 'git' user")
    else:
        raise InvalidRepositoryUrlError()
    if not parsed.hostname:
        raise InvalidRepositoryUrlError("Repository host is invalid")
    try:
        port = parsed.port
    except ValueError:
        raise InvalidRepositoryUrlError("Repository port is invalid") from None
    if parsed.query or parsed.fragment:
        raise InvalidRepositoryUrlError("Repository URL must not contain a query or fragment")
    return scheme, parsed.hostname, port, parsed.path


def parse_repository_url(
    repo_url: str,
    *,
    provider: str = AUTO_PROVIDER,
    auth_type: str | None = None,
) -> ParsedRepositoryUrl:
    scheme, raw_host, port, raw_path = _split_url(repo_url)
    host = _validate_host(raw_host)

    detected = provider_for_host(host)
    if provider == AUTO_PROVIDER:
        resolved = detected
    else:
        if provider not in PROVIDERS:
            raise InvalidRepositoryUrlError(f"Unknown git provider '{provider}'")
        resolved = provider
        if resolved in _HOST_BOUND_PROVIDERS and detected != resolved:
            spec = PROVIDERS[resolved]
            raise InvalidRepositoryUrlError(
                f"{spec.display_name} repository URLs must point at {spec.canonical_host}"
            )
    spec = PROVIDERS[resolved]

    if host in spec.hosts and spec.canonical_host:
        host = spec.canonical_host
    if port is not None and detected != "generic":
        raise InvalidRepositoryUrlError("Repository URL must not specify a port for this host")

    path = _normalize_path(raw_path, spec)

    if auth_type is not None:
        if auth_type not in AUTH_TYPES:
            raise InvalidRepositoryUrlError(f"Unknown auth type '{auth_type}'")
        if auth_type == "ssh_key" and scheme != "ssh":
            raise InvalidRepositoryUrlError(
                "SSH key authentication requires an SSH URL such as git@host:owner/repo.git"
            )
        if auth_type == "personal_access_token" and scheme != "https":
            raise InvalidRepositoryUrlError("Token authentication requires an HTTPS URL")

    authority = f"{host}:{port}" if port is not None else host
    url = f"https://{authority}/{path}" if scheme == "https" else f"ssh://git@{authority}/{path}"
    return ParsedRepositoryUrl(
        provider=resolved, scheme=scheme, host=host, port=port, path=path, url=url
    )


def sanitize_repository_url(
    repo_url: str,
    *,
    provider: str = AUTO_PROVIDER,
    auth_type: str | None = None,
) -> str:
    return parse_repository_url(repo_url, provider=provider, auth_type=auth_type).url


def detect_provider(repo_url: str | None) -> str:
    """Best-effort provider detection that never raises (``generic`` when unsure)."""
    if not repo_url:
        return "generic"
    try:
        return parse_repository_url(repo_url).provider
    except InvalidRepositoryUrlError:
        try:
            _scheme, host, _port, _path = _split_url(repo_url)
        except InvalidRepositoryUrlError:
            return "generic"
        return provider_for_host(host)


def to_ssh_url(parsed: ParsedRepositoryUrl) -> str | None:
    """SSH form of a known-provider URL (``None`` for generic hosts: SSH port/user vary)."""
    if parsed.provider == "generic":
        return None
    return f"ssh://git@{parsed.host}/{parsed.path}"


def web_url(parsed: ParsedRepositoryUrl) -> str:
    """Browser URL for the repository (``https://host/owner/repo``)."""
    authority = f"{parsed.host}:{parsed.port}" if parsed.port is not None else parsed.host
    path = parsed.path[: -len(".git")] if parsed.path.endswith(".git") else parsed.path
    return f"https://{authority}/{path}"


def redact_secrets(value: str, *secrets: str | None) -> str:
    """Mask credential material in git/CLI output regardless of host."""
    sanitized = value
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[redacted]")
    return _USERINFO.sub(r"\1://[redacted]@", sanitized)
