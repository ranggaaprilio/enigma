"""Plan 7a Task 2: every OpenSandbox sandbox is created from one safe, settings-driven request.

The live spike (plan Task 1) showed that on opensandbox-server 1.2.1 all sandboxes share a
bridge and execd accepts any caller unless it has an access token, so a sibling sandbox could
exec in another tenant's. Each sandbox therefore gets its own execd token, and an egress
policy that never lets it reach private addresses (enforced by a ``dns+nft`` server).
"""

from __future__ import annotations

import ipaddress
from typing import Any
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models import Workspace
from app.services.sandbox.base import ProvisioningError, RuntimePreviewLink
from app.services.sandbox.opensandbox import (
    EXECD_TOKEN_HEADER,
    PRIVATE_NETWORKS,
    assert_safe_create_kwargs,
    build_create_kwargs,
    execd_token,
)
from tests.sandbox.fakes import opensandbox_runtime_with

INSTALLATION = "6f0c1a52-1111-4222-8333-944455556666"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        opensandbox_api_url="http://127.0.0.1:8080",
        auth_secret_key="k" * 48,
        sandbox_network_block_all=False,
        sandbox_network_allow_list=[],
    )
    return Settings(**{**base, **overrides})


def _workspace() -> Workspace:
    return Workspace(
        id=uuid4(), sandbox_id="sbx-1", workspace_path="workspace", runtime="opensandbox"
    )


def _rules(kwargs) -> list[tuple[str, str]]:
    policy = kwargs["network_policy"]
    return [(rule.action, rule.target) for rule in policy.egress or []]


def test_a_sandbox_is_created_from_settings() -> None:
    settings = _settings()
    workspace = _workspace()

    kwargs = build_create_kwargs(settings, workspace, INSTALLATION)

    assert kwargs["image"] == "enigma-sandbox:latest"
    assert kwargs["resource"] == {"cpu": "2", "memory": "4Gi"}
    assert kwargs["metadata"] == {
        "enigma.installation": INSTALLATION,
        "enigma.workspace": str(workspace.id),
    }
    assert kwargs["env"] == {"EXECD_ACCESS_TOKEN": execd_token(settings, workspace.id)}
    assert kwargs["timeout"] is None  # Enigma owns the lifecycle
    assert_safe_create_kwargs(kwargs, settings)


def test_resources_and_image_follow_their_settings() -> None:
    settings = _settings(
        opensandbox_sandbox_image="registry.example/enigma-sandbox@sha256:" + "a" * 64,
        opensandbox_sandbox_cpu="4",
        opensandbox_sandbox_memory="8Gi",
        opensandbox_sandbox_disk="20Gi",
        opensandbox_require_image_digest=True,
    )

    kwargs = build_create_kwargs(settings, _workspace(), INSTALLATION)

    assert kwargs["image"].endswith("@sha256:" + "a" * 64)
    assert kwargs["resource"] == {"cpu": "4", "memory": "8Gi", "disk": "20Gi"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("opensandbox_sandbox_cpu", "two"),
        ("opensandbox_sandbox_memory", "4GB"),
        ("opensandbox_sandbox_disk", "-1Gi"),
    ],
)
def test_malformed_resources_are_refused_at_startup(field, value) -> None:
    with pytest.raises(ValueError):
        _settings(**{field: value})


def test_a_tag_is_refused_when_a_digest_is_required() -> None:
    settings = _settings(opensandbox_require_image_digest=True)
    with pytest.raises(ProvisioningError, match="digest"):
        build_create_kwargs(settings, _workspace(), INSTALLATION)


def test_a_digest_is_required_by_default_outside_local() -> None:
    assert Settings.model_construct(environment="production").opensandbox_image_digest_required
    assert not Settings.model_construct(environment="local").opensandbox_image_digest_required
    assert not Settings.model_construct(
        environment="production", opensandbox_require_image_digest=False
    ).opensandbox_image_digest_required


def test_the_execd_token_is_per_workspace_and_derived_from_the_secret() -> None:
    settings = _settings()
    first, second = uuid4(), uuid4()

    token = execd_token(settings, first)

    assert token == execd_token(settings, first)  # derived, never stored
    assert token != execd_token(settings, second)
    assert token != execd_token(_settings(auth_secret_key="z" * 48), first)
    assert settings.auth_secret_key not in token
    assert len(token) == 64


def _denies_every_private_network(rules) -> None:
    denied = {target for action, target in rules if action == "deny"}
    assert denied == set(PRIVATE_NETWORKS)
    for network in ("10.88.0.0/16", "172.17.0.0/16", "192.168.1.0/24", "169.254.169.254/32"):
        assert any(
            ipaddress.ip_network(network).subnet_of(ipaddress.ip_network(d))
            for d in denied
            if ipaddress.ip_network(d).version == 4
        )


def test_by_default_egress_is_open_but_never_to_private_addresses() -> None:
    kwargs = build_create_kwargs(_settings(), _workspace(), INSTALLATION)

    assert kwargs["network_policy"].default_action == "allow"
    _denies_every_private_network(_rules(kwargs))


def test_an_allow_list_denies_everything_else_and_never_opens_private_ranges() -> None:
    settings = _settings(sandbox_network_allow_list=["140.82.112.0/20", "10.0.0.0/8"])

    kwargs = build_create_kwargs(settings, _workspace(), INSTALLATION)

    assert kwargs["network_policy"].default_action == "deny"
    rules = _rules(kwargs)
    assert ("allow", "140.82.112.0/20") in rules
    assert ("allow", "10.0.0.0/8") not in rules  # a private range is never allowed
    _denies_every_private_network(rules)


def test_block_all_allows_nothing() -> None:
    kwargs = build_create_kwargs(
        _settings(sandbox_network_block_all=True, sandbox_network_allow_list=["1.1.1.1/32"]),
        _workspace(),
        INSTALLATION,
    )

    assert kwargs["network_policy"].default_action == "deny"
    assert [rule for rule in _rules(kwargs) if rule[0] == "allow"] == []


@pytest.mark.parametrize(
    "unsafe",
    [
        {"extensions": {"bootstrap.execd.isolation": "enable"}},  # SYS_ADMIN + unconfined
        {"volumes": [object()]},  # any named Docker volume on the host
        {"platform": object()},
        {"entrypoint": ["/bin/sh"]},
        {"secure_access": True},
        {"credential_proxy": object()},
        {"image": "attacker/image:latest"},
        {"env": {"EXECD_ACCESS_TOKEN": "x", "LD_PRELOAD": "/tmp/x.so"}},
        {"network_policy": None},
    ],
)
def test_unsafe_create_requests_are_refused(unsafe) -> None:
    settings = _settings()
    kwargs = {**build_create_kwargs(settings, _workspace(), INSTALLATION), **unsafe}

    with pytest.raises(ProvisioningError):
        assert_safe_create_kwargs(kwargs, settings)


# --- the runtime uses it: create, connect, previews and terminals carry the token ----------


async def test_provision_sends_the_safe_request_and_never_logs_the_token(monkeypatch) -> None:
    from app.services.sandbox import opensandbox as module

    logged: list[str] = []
    for level in ("debug", "info", "warning", "error", "exception"):
        monkeypatch.setattr(
            module.logger, level, lambda event, **kw: logged.append(f"{event} {kw!r}")
        )
    runtime = opensandbox_runtime_with()
    runtime.settings = _settings()
    runtime._installation = INSTALLATION
    sent: list[dict] = []
    fake = runtime._fake_sandbox

    async def create(kwargs: dict) -> Any:
        sent.append(kwargs)
        return fake

    runtime._create = create
    workspace = _workspace()

    await runtime.provision(workspace)

    [kwargs] = sent
    token = execd_token(runtime.settings, workspace.id)
    assert kwargs["env"]["EXECD_ACCESS_TOKEN"] == token
    assert token not in repr(logged)


async def test_every_connection_to_a_sandbox_carries_its_execd_token() -> None:
    runtime = opensandbox_runtime_with()
    runtime.settings = _settings()
    seen: list[dict] = []
    fake = runtime._fake_sandbox

    async def connect(sandbox_id: str, *, headers: dict[str, str]) -> Any:
        seen.append(headers)
        return fake

    runtime._connect = connect
    workspace = _workspace()

    await runtime.exec(workspace, "true")

    assert seen == [{EXECD_TOKEN_HEADER: execd_token(runtime.settings, workspace.id)}]


async def test_a_preview_link_carries_the_execd_token_header() -> None:
    runtime = opensandbox_runtime_with()
    runtime.settings = _settings()
    workspace = _workspace()

    link = await runtime.preview_link(workspace, 5173)

    assert link.token == ""  # no Daytona-style token to renew
    assert link.headers == {EXECD_TOKEN_HEADER: execd_token(runtime.settings, workspace.id)}


async def test_terminal_calls_to_execd_carry_the_token() -> None:
    runtime = opensandbox_runtime_with()
    runtime.settings = _settings()
    workspace = _workspace()
    seen: list[dict] = []
    seams = ("_pty_create_session", "_pty_session_status", "_pty_connect", "_pty_delete_session")
    for name in seams:
        original = getattr(runtime, name)

        def wrap(original=original):
            async def call(*args, headers, **kwargs):
                seen.append(headers)
                return await original(*args, headers=headers, **kwargs)

            return call

        setattr(runtime, name, wrap())

    async def on_data(_: bytes) -> None:
        return None

    await runtime.create_or_connect_pty(workspace, "t1", on_data, env=None, cols=80, rows=24)
    await runtime.kill_pty(workspace, "t1")

    expected = {EXECD_TOKEN_HEADER: execd_token(runtime.settings, workspace.id)}
    assert len(seen) == 4 and all(headers == expected for headers in seen)


def test_the_preview_proxy_forwards_provider_headers() -> None:
    from app.preview_proxy import _sandbox_token_headers

    link = RuntimePreviewLink(
        url="http://h/proxy/5173", token="", headers={EXECD_TOKEN_HEADER: "t"}
    )
    assert _sandbox_token_headers(link) == {EXECD_TOKEN_HEADER: "t"}
    daytona = RuntimePreviewLink(url="https://p", token="d")
    assert _sandbox_token_headers(daytona)["X-Daytona-Preview-Token"] == "d"


async def test_the_observation_fetch_sends_provider_headers() -> None:
    import httpx

    from app.services.verification.observation import _capture_snapshot

    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(EXECD_TOKEN_HEADER))
        return httpx.Response(200, text="<title>ok</title>")

    link = RuntimePreviewLink(
        url="http://h/proxy/5173", token="", headers={EXECD_TOKEN_HEADER: "t"}
    )
    await _capture_snapshot(link, transport=httpx.MockTransport(handler))

    assert seen == ["t"]


# --- security review fixes ------------------------------------------------------------------


def test_the_execd_token_uses_its_own_key_when_one_is_set() -> None:
    workspace_id = uuid4()
    shared = execd_token(_settings(), workspace_id)
    dedicated = execd_token(_settings(opensandbox_execd_token_key="e" * 48), workspace_id)
    assert dedicated != shared
    # Rotating the session secret then leaves every sandbox's token unchanged.
    assert dedicated == execd_token(
        _settings(opensandbox_execd_token_key="e" * 48, auth_secret_key="z" * 48), workspace_id
    )


def test_a_dedicated_execd_key_is_required_outside_local() -> None:
    settings = Settings.model_construct(
        environment="production",
        opensandbox_api_url="http://os:8080",
        opensandbox_execd_token_key="",
    )
    assert "opensandbox_execd_token_key" in settings.insecure_setting_names()
    configured = Settings.model_construct(
        environment="production",
        opensandbox_api_url="http://os:8080",
        opensandbox_execd_token_key="e" * 48,
    )
    assert "opensandbox_execd_token_key" not in configured.insecure_setting_names()


def test_ipv4_mapped_and_hostname_allow_list_entries_never_open_anything() -> None:
    settings = _settings(
        sandbox_network_allow_list=["::ffff:10.0.0.0/104", "internal.example.com", "8.8.8.8/32"]
    )

    rules = _rules(build_create_kwargs(settings, _workspace(), INSTALLATION))

    assert [target for action, target in rules if action == "allow"] == ["8.8.8.8/32"]
    assert ("deny", "::ffff:0:0/96") in rules


async def test_the_observation_never_sends_provider_headers_to_another_origin() -> None:
    import httpx

    from app.services.verification.observation import _capture_snapshot

    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get(EXECD_TOKEN_HEADER)))
        if request.url.path == "/proxy/5173/":
            return httpx.Response(302, headers={"location": "/proxy/5173/home"})
        if request.url.path == "/proxy/5173/home":
            return httpx.Response(302, headers={"location": "http://attacker.example/steal"})
        return httpx.Response(200, text="<title>away</title>")

    link = RuntimePreviewLink(
        url="http://h/proxy/5173/", token="", headers={EXECD_TOKEN_HEADER: "t"}
    )
    await _capture_snapshot(link, transport=httpx.MockTransport(handler))

    # The app's own redirect is followed; one to another origin is never requested at all
    # (the backend is not an open fetcher of tenant-chosen URLs).
    assert seen == [("h", "t"), ("h", "t")]


def test_the_preview_proxy_drops_a_visitor_supplied_execd_token() -> None:
    from app.preview_proxy import REQUEST_HEADERS_TO_STRIP

    assert "x-execd-access-token" in REQUEST_HEADERS_TO_STRIP


# --- Task 3: the security floor is checked in the live sandbox ------------------------------

HEALTHY_FLOOR = "CapEff: 0000000000000000\nNoNewPrivs: 1\n1001\n"


def _floor_runtime(floor: str):
    from app.services.sandbox.opensandbox import FLOOR_PROBE

    runtime = opensandbox_runtime_with()
    runtime.settings = _settings()
    fake = runtime._fake_sandbox
    fake.floor = floor
    probes = lambda: [c for c, *_ in fake.commands.run_calls if c == FLOOR_PROBE]  # noqa: E731
    return runtime, fake, probes


@pytest.mark.parametrize(
    ("floor", "named"),
    [
        ("CapEff: 00000000a80425fb\nNoNewPrivs: 1\n1001\n", "CapEff"),  # caps not all dropped
        ("CapEff: 0000000000000000\nNoNewPrivs: 0\n1001\n", "NoNewPrivs"),
        ("CapEff: 0000000000000000\nNoNewPrivs: 1\n0\n", "root"),
        ("", "CapEff"),  # the probe printed nothing: never assume the floor
    ],
)
async def test_a_new_sandbox_below_the_floor_is_destroyed_and_refused(floor, named) -> None:
    runtime, fake, _ = _floor_runtime(floor)

    with pytest.raises(ProvisioningError, match=named):
        await runtime.provision(_workspace())

    assert fake.destroyed


async def test_a_new_sandbox_at_the_floor_is_kept_and_not_probed_again() -> None:
    runtime, fake, probes = _floor_runtime(HEALTHY_FLOOR)
    workspace = _workspace()

    workspace.sandbox_id = await runtime.provision(workspace)
    await runtime.exec(workspace, "true")

    assert not fake.destroyed
    assert len(probes()) == 1


async def test_an_existing_sandbox_is_checked_once_per_process() -> None:
    runtime, _, probes = _floor_runtime(HEALTHY_FLOOR)
    workspace = _workspace()

    await runtime.exec(workspace, "true")
    await runtime.exec(workspace, "true")

    assert len(probes()) == 1


async def test_an_existing_sandbox_below_the_floor_is_refused_but_kept() -> None:
    from app.services.sandbox.base import SandboxRuntimeError

    runtime, fake, _ = _floor_runtime("CapEff: 00000000a80425fb\nNoNewPrivs: 1\n1001\n")

    with pytest.raises(SandboxRuntimeError, match="CapEff"):
        await runtime.exec(_workspace(), "true")
    assert not fake.destroyed  # the user's files stay; recreate the workspace to fix it


# --- preview paths never leave the preview's own prefix --------------------------------------
# OpenSandbox's preview URL is execd's own origin (`http://host:<execd>/proxy/<port>`), so a
# path that climbed out of `/proxy/<port>` would reach execd's control API with the sandbox
# token: a preview visitor could then run commands in the sandbox.


@pytest.mark.parametrize(
    "path",
    [
        "../../files/download",
        "assets/../../../command",
        "%2e%2e/%2e%2e/files/download",
        "%2E%2e%2F%2e%2E%2Fcommand",
        "..%2f..%2fcommand",
        "a\\..\\..\\command",
        "%252e%252e/%252e%252e/command",
        ".",
    ],
)
def test_a_preview_path_cannot_climb_out_of_its_prefix(path) -> None:
    from app.preview_proxy import UnsafePreviewPathError, _upstream_url

    with pytest.raises(UnsafePreviewPathError):
        _upstream_url("http://127.0.0.1:46589/proxy/5173", path, "")


@pytest.mark.parametrize("path", ["", "assets/app.js", "a.b/c..d/x", "api/v1/items", "%20space"])
def test_ordinary_preview_paths_stay_under_the_prefix(path) -> None:
    import httpx

    from app.preview_proxy import _upstream_url

    url = _upstream_url("http://127.0.0.1:46589/proxy/5173", path, "q=1")
    assert httpx.URL(url).path.startswith("/proxy/5173/")


async def test_a_sandbox_below_the_floor_can_always_be_deleted() -> None:
    """Teardown never runs the floor check: an unsafe sandbox must stay removable."""
    runtime, fake, probes = _floor_runtime("CapEff: 00000000a80425fb\nNoNewPrivs: 1\n1001\n")

    await runtime.delete(_workspace())

    assert fake.destroyed
    assert probes() == []


# --- Task 4: an OpenSandbox server that does not authenticate is never offered ---------------


def _auth_runtime(*, probe_status: int | None, **settings: Any):
    runtime = opensandbox_runtime_with()
    runtime.settings = _settings(**settings)
    probes: list[bool] = []

    async def auth_probe_status() -> int:
        probes.append(True)
        assert probe_status is not None, "the probe must not run"
        return probe_status

    runtime._auth_probe_status = auth_probe_status
    return runtime, probes


async def test_a_server_without_an_api_key_is_insecure() -> None:
    from app.services.sandbox.availability import check_runtime

    runtime, probes = _auth_runtime(probe_status=None, opensandbox_api_key=None)

    found = await check_runtime(runtime, "opensandbox")

    assert (found.status, found.available) == ("insecure", False)
    assert found.reason == "The OpenSandbox server does not require authentication"
    assert probes == []


async def test_a_server_that_answers_without_the_key_is_insecure() -> None:
    from app.services.sandbox.availability import check_runtime

    runtime, probes = _auth_runtime(probe_status=200, opensandbox_api_key="s3cret-key")

    found = await check_runtime(runtime, "opensandbox")

    assert found.status == "insecure"
    assert "s3cret-key" not in (found.reason or "")
    assert probes == [True]


async def test_a_server_that_refuses_requests_without_the_key_is_ok() -> None:
    from app.services.sandbox.availability import check_runtime

    runtime, _ = _auth_runtime(probe_status=401, opensandbox_api_key="s3cret-key")

    assert (await check_runtime(runtime, "opensandbox")).status == "ok"


async def test_a_dev_install_may_allow_an_insecure_server(monkeypatch) -> None:
    from app.services.sandbox import opensandbox as module
    from app.services.sandbox.availability import check_runtime

    warnings: list[str] = []
    # structlog caches a module logger on first use, so capture_logs() would depend on test
    # order; record the module logger's warnings directly instead.
    monkeypatch.setattr(module.logger, "warning", lambda event, **_: warnings.append(event))
    runtime, probes = _auth_runtime(
        probe_status=None, opensandbox_api_key=None, opensandbox_allow_insecure=True
    )

    found = await check_runtime(runtime, "opensandbox")

    assert found.status == "ok"
    assert probes == []
    assert "opensandbox_insecure_server_allowed" in warnings


def test_allowing_an_insecure_server_is_refused_outside_local() -> None:
    settings = Settings.model_construct(
        environment="production",
        opensandbox_api_url="http://os:8080",
        opensandbox_execd_token_key="e" * 48,
        opensandbox_allow_insecure=True,
    )
    assert "opensandbox_allow_insecure" in settings.insecure_setting_names()
