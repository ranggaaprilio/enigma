import pytest

from app.services.git_providers import (
    InvalidRepositoryUrlError,
    detect_provider,
    http_basic_username,
    parse_repository_url,
    redact_secrets,
    sanitize_repository_url,
    supports_pull_requests,
    to_ssh_url,
    web_url,
)


@pytest.mark.parametrize(
    ("repo_url", "expected"),
    [
        ("https://github.com/example/demo", "github"),
        ("https://www.github.com/example/demo.git", "github"),
        ("https://gitlab.com/group/sub/demo", "gitlab"),
        ("git@bitbucket.org:team/demo.git", "bitbucket"),
        ("https://git.example.com/team/demo.git", "generic"),
        ("ssh://git@git.example.com:2222/team/demo.git", "generic"),
        ("not a url", "generic"),
        ("", "generic"),
        (None, "generic"),
    ],
)
def test_detect_provider_by_host(repo_url, expected):
    assert detect_provider(repo_url) == expected


def test_parse_https_normalizes_and_strips_userinfo():
    parsed = parse_repository_url("https://ghp_secret@WWW.GitHub.com/Example/Demo/")
    assert parsed.provider == "github"
    assert parsed.scheme == "https"
    assert parsed.host == "github.com"
    assert parsed.path == "Example/Demo.git"
    assert parsed.url == "https://github.com/Example/Demo.git"
    assert "ghp_secret" not in parsed.url


def test_parse_scp_like_ssh_url_canonicalizes_to_ssh_scheme():
    parsed = parse_repository_url("git@github.com:example/demo.git")
    assert parsed.scheme == "ssh"
    assert parsed.url == "ssh://git@github.com/example/demo.git"
    assert parsed.provider == "github"


def test_parse_ssh_scheme_with_port_generic_only():
    parsed = parse_repository_url("ssh://git@git.example.com:2222/team/demo")
    assert parsed.port == 2222
    assert parsed.url == "ssh://git@git.example.com:2222/team/demo.git"
    with pytest.raises(InvalidRepositoryUrlError):
        parse_repository_url("https://github.com:8443/example/demo")


def test_gitlab_nested_groups_allowed_but_github_requires_two_segments():
    parsed = parse_repository_url("https://gitlab.com/group/subgroup/demo")
    assert parsed.path == "group/subgroup/demo.git"
    with pytest.raises(InvalidRepositoryUrlError):
        parse_repository_url("https://github.com/example/demo/issues")
    with pytest.raises(InvalidRepositoryUrlError):
        parse_repository_url("https://github.com/example")


def test_explicit_gitlab_provider_accepts_self_hosted_host():
    parsed = parse_repository_url("https://gitlab.acme.dev/team/demo", provider="gitlab")
    assert parsed.provider == "gitlab"
    assert parsed.url == "https://gitlab.acme.dev/team/demo.git"


def test_explicit_github_provider_requires_github_host():
    with pytest.raises(InvalidRepositoryUrlError) as excinfo:
        parse_repository_url("https://gitlab.com/team/demo", provider="github")
    assert "github.com" in excinfo.value.message


@pytest.mark.parametrize(
    "repo_url",
    [
        "http://github.com/example/demo",
        "git://github.com/example/demo.git",
        "file:///tmp/repo.git",
        "ftp://github.com/example/demo",
        "root@github.com:example/demo.git",
        "https://github.com/example/demo?ref=main",
    ],
)
def test_rejects_unsupported_schemes_users_and_queries(repo_url):
    with pytest.raises(InvalidRepositoryUrlError):
        parse_repository_url(repo_url)


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://127.0.0.1/a/b",
        "https://[::1]/a/b",
        "https://localhost/a/b",
        "https://gitlab/a/b",
        "https://foo.internal/a/b",
        "https://169.254.169.254/x/y",
        "https://10.0.0.5:8443/x/y",
        "https://db.local/x/y",
        "git@intranet.lan:x/y.git",
    ],
)
def test_rejects_ip_literals_localhost_internal_and_dotless_hosts(repo_url):
    with pytest.raises(InvalidRepositoryUrlError):
        parse_repository_url(repo_url)


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://github.com/../demo",
        "https://github.com/example/..",
        "https://git.example.com/team/de mo",
        "https://git.example.com/team/demo%2F..",
    ],
)
def test_rejects_path_traversal_segments(repo_url):
    with pytest.raises(InvalidRepositoryUrlError):
        parse_repository_url(repo_url)


def test_ssh_key_auth_requires_ssh_url():
    with pytest.raises(InvalidRepositoryUrlError) as excinfo:
        parse_repository_url("https://github.com/example/demo", auth_type="ssh_key")
    assert "SSH URL" in excinfo.value.message
    parsed = parse_repository_url("git@github.com:example/demo", auth_type="ssh_key")
    assert parsed.scheme == "ssh"


def test_token_auth_requires_https_url():
    with pytest.raises(InvalidRepositoryUrlError) as excinfo:
        parse_repository_url("git@github.com:example/demo", auth_type="personal_access_token")
    assert "HTTPS" in excinfo.value.message


def test_sanitize_repository_url_returns_canonical_url():
    assert sanitize_repository_url("https://gitlab.com/team/demo") == (
        "https://gitlab.com/team/demo.git"
    )


def test_to_ssh_url_known_providers_and_none_for_generic():
    github = parse_repository_url("https://github.com/example/demo")
    assert to_ssh_url(github) == "ssh://git@github.com/example/demo.git"
    generic = parse_repository_url("https://git.example.com/team/demo")
    assert to_ssh_url(generic) is None


def test_web_url_strips_git_suffix():
    parsed = parse_repository_url("ssh://git@git.example.com:2222/team/demo.git")
    assert web_url(parsed) == "https://git.example.com:2222/team/demo"


def test_http_basic_username_per_provider_with_override():
    assert http_basic_username("github") == "x-access-token"
    assert http_basic_username("gitlab") == "oauth2"
    assert http_basic_username("bitbucket") == "x-token-auth"
    assert http_basic_username("generic") == "git"
    assert http_basic_username("bitbucket", "alice") == "alice"
    assert http_basic_username("bitbucket", "   ") == "x-token-auth"


def test_supports_pull_requests_only_for_github():
    assert supports_pull_requests("github") is True
    assert supports_pull_requests("gitlab") is False
    assert supports_pull_requests("generic") is False
    assert supports_pull_requests(None) is False


def test_redact_secrets_masks_any_host_and_verbatim_secrets():
    output = (
        "fatal: https://oauth2:glpat-abc@gitlab.com/team/demo.git failed; "
        "ssh://git@git.example.com/x.git; token glpat-abc leaked"
    )
    redacted = redact_secrets(output, "glpat-abc", None)
    assert "glpat-abc" not in redacted
    assert "https://[redacted]@gitlab.com/team/demo.git" in redacted
    assert "ssh://[redacted]@git.example.com/x.git" in redacted
