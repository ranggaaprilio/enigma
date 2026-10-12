"""Guards on the production packaging and Kubernetes manifests (Epic 08).

Pure file checks -- no cluster, registry or container runtime needed. They pin the properties a
zero-downtime, autoscaling deploy relies on (probes, rollout strategy, grace periods, secret
hygiene, network policy, the worker queue-depth query) so an edit that breaks one fails CI
instead of the next rollout. Rendering and schema validation of the manifests happen in the CI
`deploy` job (kustomize + kubeconform); these tests cover what a schema cannot.
"""

import re
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from app.models.agent import AgentRun
from app.models.command import CommandRun
from app.services.capacity import ACTIVE_COMMAND_RUN_STATUS, ACTIVE_RUN_STATUSES

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "deploy" / "k8s" / "base"
PRODUCTION = REPO / "deploy" / "k8s" / "overlays" / "production"

# Values that must only ever reach a pod from a Secret, never from a literal or a ConfigMap.
SECRET_ENV_NAMES = {
    "AUTH_SECRET_KEY",
    "WORKSPACE_ENV_ENCRYPTION_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "METRICS_TOKEN",
    "DAYTONA_API_KEY",
    "OPENAI_API_KEY",
    "OPENHANDS_LLM_API_KEY",
}
SECRET_NAME_PATTERN = re.compile(r"SECRET|PASSWORD|TOKEN|_KEY\b|API_KEY", re.IGNORECASE)

# Slack on top of the app's own shutdown budget before the kubelet sends SIGKILL.
GRACE_MARGIN_SECONDS = 10


# --- helpers ------------------------------------------------------------------------------


def _documents(directory: Path) -> list[dict]:
    docs: list[dict] = []
    for path in sorted(directory.glob("*.yaml")):
        if path.name == "kustomization.yaml":
            continue
        docs.extend(doc for doc in yaml.safe_load_all(path.read_text()) if doc)
    return docs


@pytest.fixture(scope="module")
def manifests() -> list[dict]:
    return _documents(BASE)


def _get(manifests: list[dict], kind: str, name: str) -> dict:
    matches = [m for m in manifests if m["kind"] == kind and m["metadata"]["name"] == name]
    assert len(matches) == 1, f"expected exactly one {kind}/{name}, found {len(matches)}"
    return matches[0]


def _of_kind(manifests: list[dict], kind: str) -> list[dict]:
    return [m for m in manifests if m["kind"] == kind]


def _pod_spec(workload: dict) -> dict:
    return workload["spec"]["template"]["spec"]


def _container(workload: dict) -> dict:
    containers = _pod_spec(workload)["containers"]
    assert len(containers) == 1
    return containers[0]


def _env(container: dict) -> dict[str, dict]:
    return {item["name"]: item for item in container.get("env", [])}


def _config(manifests: list[dict]) -> dict[str, str]:
    return _get(manifests, "ConfigMap", "enigma-config")["data"]


def _setting(manifests: list[dict], container: dict, name: str, default: float) -> float:
    """A setting as the container will see it: its own env, then the ConfigMap, then default."""
    env = _env(container)
    if name in env and "value" in env[name]:
        return float(env[name]["value"])
    config = _config(manifests)
    if name in config:
        return float(config[name])
    return float(default)


def _final_stage(dockerfile: str) -> str:
    return re.split(r"^FROM\s", dockerfile, flags=re.MULTILINE)[-1]


def _stage_names(dockerfile: str) -> set[str]:
    return set(re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", dockerfile, flags=re.MULTILINE | re.I))


WORKLOADS = ("enigma-api", "enigma-worker", "enigma-preview-proxy", "enigma-frontend")
SERVED = ("enigma-api", "enigma-preview-proxy", "enigma-frontend")


# --- images -------------------------------------------------------------------------------


def test_backend_runtime_image_is_production_shaped():
    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    final = _final_stage(dockerfile)

    assert "--reload" not in final
    assert "uv run" not in final, "PID 1 must be the server itself so SIGTERM reaches it"
    user = re.findall(r"^USER\s+(\S+)", final, flags=re.MULTILINE)
    assert user and user[-1] not in {"root", "0", "0:0"}
    # compose builds the `dev` target, which keeps uv for its `uv sync` + `--reload` loop.
    assert "dev" in _stage_names(dockerfile)


@pytest.mark.parametrize("dockerfile", ["backend/Dockerfile", "frontend/Dockerfile"])
def test_images_bake_no_secrets(dockerfile: str):
    text = (REPO / dockerfile).read_text()
    for name in re.findall(r"^(?:ENV|ARG)\s+([A-Za-z_][A-Za-z0-9_]*)", text, flags=re.M):
        assert not SECRET_NAME_PATTERN.search(name), f"{dockerfile} bakes {name} into a layer"
    assert not re.search(r"^COPY\s+.*\.env\b", text, flags=re.MULTILINE)


@pytest.mark.parametrize("context", ["backend", "frontend"])
def test_dockerignore_keeps_env_files_out_of_the_build_context(context: str):
    ignored = (REPO / context / ".dockerignore").read_text().split()
    assert ".env" in ignored and ".env.*" in ignored


def test_frontend_runtime_image_serves_a_static_build():
    dockerfile = (REPO / "frontend" / "Dockerfile").read_text()
    final = _final_stage(dockerfile)

    assert "pnpm install --frozen-lockfile" in dockerfile
    assert "vite build" in dockerfile or "pnpm run build" in dockerfile
    assert "nginx" in final.splitlines()[0]
    assert "vite.js" not in final, "the runtime image must not run the Vite dev server"
    assert "dev" in _stage_names(dockerfile)
    # An empty VITE_API_BASE_URL is not nullish, so the bundle would silently call its own
    # origin (nginx answers every path with index.html). The build must refuse instead.
    assert 'test -n "$VITE_API_BASE_URL"' in dockerfile


def test_frontend_nginx_cache_busts_on_release():
    conf = (REPO / "frontend" / "nginx.conf").read_text()

    # Vite fingerprints everything under /assets, so it can be cached forever; index.html names
    # the current fingerprints and must be revalidated on every load.
    assets = re.search(r"location\s+/assets/\s*\{([^}]*)\}", conf)
    assert assets and "immutable" in assets.group(1)
    # Not `always`: mid-rollout a new index.html can ask an old pod for an asset it lacks, and
    # that 404 must not be cached for a year by the browser or a CDN.
    immutable = next(line for line in assets.group(1).splitlines() if "immutable" in line)
    assert not immutable.rstrip().rstrip(";").endswith("always")
    index = re.search(r"location\s+=\s+/index\.html\s*\{([^}]*)\}", conf)
    assert index and "no-cache" in index.group(1)
    assert re.search(r"try_files\s+\$uri\s+\$uri/\s+/index\.html", conf), "SPA fallback"
    assert re.search(r"location\s+=\s+/healthz", conf)


def test_compose_keeps_building_the_dev_targets():
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    for service in ("backend", "worker", "preview-proxy", "frontend"):
        assert compose["services"][service]["build"].get("target") == "dev", service


# --- kustomize layout ---------------------------------------------------------------------


def test_base_kustomization_lists_every_manifest():
    kustomization = yaml.safe_load((BASE / "kustomization.yaml").read_text())
    on_disk = {p.name for p in BASE.glob("*.yaml")} - {"kustomization.yaml"}
    assert set(kustomization["resources"]) == on_disk


def test_production_overlay_builds_on_base_and_pins_images():
    kustomization = yaml.safe_load((PRODUCTION / "kustomization.yaml").read_text())
    assert "../../base" in kustomization["resources"]
    pinned = {image["name"]: image for image in kustomization["images"]}
    for name in ("enigma-backend", "enigma-frontend"):
        assert pinned[name].get("newTag") not in (None, "", "latest")


# --- secrets & config ---------------------------------------------------------------------


def test_no_secret_material_in_manifests(manifests):
    assert not _of_kind(manifests, "Secret"), "secrets come from the cluster's secret store"
    assert not SECRET_ENV_NAMES & set(_config(manifests))
    for workload in _of_kind(manifests, "Deployment") + _of_kind(manifests, "Job"):
        for name, item in _env(_container(workload)).items():
            if name in SECRET_ENV_NAMES:
                assert "secretKeyRef" in item.get("valueFrom", {}), (workload, name)
        # Bulk imports come only from the one ConfigMap (checked above) or a Secret.
        for source in _container(workload).get("envFrom", []):
            if "configMapRef" in source:
                assert source["configMapRef"]["name"] == "enigma-config"
            else:
                assert "secretRef" in source, (workload["metadata"]["name"], source)


@pytest.mark.parametrize("name", SERVED)
def test_services_route_to_their_deployment(manifests, name):
    # kubeconform cannot see this: a renamed port or label passes CI and blackholes traffic.
    service = _get(manifests, "Service", name)
    workload = _get(manifests, "Deployment", name)
    pod_labels = workload["spec"]["template"]["metadata"]["labels"]
    assert service["spec"]["selector"].items() <= pod_labels.items()
    port_names = {p["name"] for p in _container(workload)["ports"]}
    assert {p["targetPort"] for p in service["spec"]["ports"]} <= port_names


def test_cluster_runs_in_queue_mode_outside_local(manifests):
    config = _config(manifests)
    # More than one API replica is only safe in queue mode: inline startup reconciliation
    # assumes it is the only replica and would end the other replicas' runs.
    assert config["RUN_EXECUTION_MODE"] == "queue"
    assert config["ENVIRONMENT"] != "local"


# --- workloads ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", WORKLOADS)
def test_workloads_are_locked_down(manifests, name):
    workload = _get(manifests, "Deployment", name)
    pod = _pod_spec(workload)
    container = _container(workload)
    security = container["securityContext"]

    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert security["allowPrivilegeEscalation"] is False
    assert security["readOnlyRootFilesystem"] is True
    assert security["capabilities"]["drop"] == ["ALL"]
    # HPA utilisation targets are a percentage of the request.
    assert container["resources"]["requests"]["cpu"]
    assert container["resources"]["limits"]["memory"]
    assert container["image"].split(":")[0] in {"enigma-backend", "enigma-frontend"}


@pytest.mark.parametrize("name", SERVED)
def test_served_tiers_roll_without_losing_capacity(manifests, name):
    workload = _get(manifests, "Deployment", name)
    strategy = workload["spec"]["strategy"]
    assert strategy["type"] == "RollingUpdate"
    assert strategy["rollingUpdate"]["maxUnavailable"] == 0
    assert strategy["rollingUpdate"]["maxSurge"] >= 1

    container = _container(workload)
    port = container["ports"][0]["containerPort"]
    readiness = container["readinessProbe"]["httpGet"]
    liveness = container["livenessProbe"]["httpGet"]
    assert readiness["port"] == port and liveness["port"] == port
    if name == "enigma-frontend":
        assert readiness["path"] == liveness["path"] == "/healthz"
    else:
        # /ready gates traffic during startup and drain; /live never checks a dependency, so a
        # Postgres blip does not restart every replica at once.
        assert readiness["path"] == "/ready"
        assert liveness["path"] == "/live"
        assert container["startupProbe"]["httpGet"]["path"] == "/live"


def test_api_grace_period_covers_the_drain(manifests):
    api = _get(manifests, "Deployment", "enigma-api")
    container = _container(api)
    defaults = Settings.model_fields
    drain = _setting(
        manifests,
        container,
        "SHUTDOWN_DRAIN_DELAY_SECONDS",
        defaults["shutdown_drain_delay_seconds"].default,
    )
    grace = _setting(
        manifests,
        container,
        "SHUTDOWN_RUN_GRACE_SECONDS",
        defaults["shutdown_run_grace_seconds"].default,
    )
    assert _pod_spec(api)["terminationGracePeriodSeconds"] >= drain + grace + GRACE_MARGIN_SECONDS
    # The readiness probe must observe /ready=503 inside the drain delay, or the endpoint is
    # still routed when the listener closes.
    probe = container["readinessProbe"]
    assert probe["periodSeconds"] * probe["failureThreshold"] <= drain


def test_preview_proxy_grace_period_covers_the_drain(manifests):
    proxy = _get(manifests, "Deployment", "enigma-preview-proxy")
    container = _container(proxy)
    command = " ".join(container.get("command", []) + container.get("args", []))
    match = re.search(r"--timeout-graceful-shutdown[ =](\d+)", command)
    assert match, "in-flight proxied streams need a bounded graceful shutdown"
    drain = _setting(
        manifests,
        container,
        "SHUTDOWN_DRAIN_DELAY_SECONDS",
        Settings.model_fields["shutdown_drain_delay_seconds"].default,
    )
    budget = drain + int(match.group(1)) + GRACE_MARGIN_SECONDS
    assert _pod_spec(proxy)["terminationGracePeriodSeconds"] >= budget
    probe = container["readinessProbe"]
    assert probe["periodSeconds"] * probe["failureThreshold"] <= drain


def test_worker_drain_lets_in_flight_runs_finish(manifests):
    worker = _get(manifests, "Deployment", "enigma-worker")
    container = _container(worker)
    job_timeout = _setting(
        manifests,
        container,
        "WORKER_JOB_TIMEOUT_SECONDS",
        Settings.model_fields["worker_job_timeout_seconds"].default,
    )
    # arq's job_completion_wait is SHUTDOWN_RUN_GRACE_SECONDS: a worker being rolled or scaled
    # in stops taking jobs and lets its runs finish instead of interrupting them.
    grace = _setting(manifests, container, "SHUTDOWN_RUN_GRACE_SECONDS", 0)
    assert grace >= job_timeout
    assert _pod_spec(worker)["terminationGracePeriodSeconds"] >= grace + GRACE_MARGIN_SECONDS
    assert worker["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
    assert "app.worker.WorkerSettings" in " ".join(container.get("args", []))


def test_writable_tmp_for_read_only_root_filesystems(manifests):
    for name in WORKLOADS:
        workload = _get(manifests, "Deployment", name)
        mounts = {m["mountPath"] for m in _container(workload).get("volumeMounts", [])}
        assert "/tmp" in mounts, name


@pytest.mark.parametrize("name", SERVED)
def test_disruption_budgets_keep_a_replica_up(manifests, name):
    budget = _get(manifests, "PodDisruptionBudget", name)
    workload = _get(manifests, "Deployment", name)
    selector = workload["spec"]["selector"]["matchLabels"]
    assert budget["spec"]["selector"]["matchLabels"] == selector
    assert budget["spec"].get("maxUnavailable") == 1 or budget["spec"].get("minAvailable") == 1


def test_served_tiers_spread_across_nodes(manifests):
    for name in SERVED:
        spread = _pod_spec(_get(manifests, "Deployment", name))["topologySpreadConstraints"]
        assert spread[0]["topologyKey"] == "kubernetes.io/hostname"


# --- autoscaling --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["enigma-api", "enigma-preview-proxy"])
def test_stateless_tiers_autoscale_on_cpu(manifests, name):
    hpa = _get(manifests, "HorizontalPodAutoscaler", name)
    spec = hpa["spec"]
    assert spec["scaleTargetRef"] == {"apiVersion": "apps/v1", "kind": "Deployment", "name": name}
    # Two replicas minimum: killing any single pod must not take the tier down.
    assert spec["minReplicas"] >= 2
    assert spec["maxReplicas"] > spec["minReplicas"]
    assert any(m["resource"]["name"] == "cpu" for m in spec["metrics"])
    assert spec["behavior"]["scaleDown"]["stabilizationWindowSeconds"] >= 300
    # The HPA owns the replica count; a hardcoded one would fight it on every apply.
    assert "replicas" not in _get(manifests, "Deployment", name)["spec"]


def test_workers_autoscale_on_queue_depth(manifests):
    scaled = _get(manifests, "ScaledObject", "enigma-worker")
    spec = scaled["spec"]
    assert spec["scaleTargetRef"]["name"] == "enigma-worker"
    assert spec["minReplicaCount"] >= 1
    assert spec["maxReplicaCount"] > spec["minReplicaCount"]
    assert "replicas" not in _get(manifests, "Deployment", "enigma-worker")["spec"]

    (trigger,) = spec["triggers"]
    assert trigger["type"] == "postgresql"
    assert trigger["authenticationRef"]["name"] == "enigma-worker-keda"
    query = trigger["metadata"]["query"]
    # Postgres is authoritative for the queue (gotcha 23): count the same rows capacity.py
    # counts -- runs handed to the queue that are still active, waiting or executing.
    for table in (AgentRun.__tablename__, CommandRun.__tablename__):
        assert re.search(rf"\bFROM\s+{table}\b", query, flags=re.I), table
    for status in (*ACTIVE_RUN_STATUSES, ACTIVE_COMMAND_RUN_STATUS):
        assert f"'{status}'" in query
    assert "enqueued_at IS NOT NULL" in query

    worker = _container(_get(manifests, "Deployment", "enigma-worker"))
    max_jobs = _setting(
        manifests, worker, "WORKER_MAX_JOBS", Settings.model_fields["worker_max_jobs"].default
    )
    assert float(trigger["metadata"]["targetQueryValue"]) <= max_jobs

    auth = _get(manifests, "TriggerAuthentication", "enigma-worker-keda")
    (param,) = auth["spec"]["secretTargetRef"]
    assert param["parameter"] == "connection"

    # A broken trigger (missing secret, revoked SELECT) must not pin the fleet at the minimum.
    fallback = spec["fallback"]
    assert fallback["failureThreshold"] >= 1
    assert spec["minReplicaCount"] < fallback["replicas"] <= spec["maxReplicaCount"]


# --- edge & network -----------------------------------------------------------------------

NGINX = "nginx.ingress.kubernetes.io"


def _backends(ingress: dict) -> dict[str, str]:
    return {
        rule["host"]: rule["http"]["paths"][0]["backend"]["service"]["name"]
        for rule in ingress["spec"]["rules"]
    }


@pytest.mark.parametrize("name", ["enigma", "enigma-preview"])
def test_ingress_terminates_tls_without_affinity(manifests, name):
    ingress = _get(manifests, "Ingress", name)
    annotations = ingress["metadata"]["annotations"]
    assert annotations[f"{NGINX}/force-ssl-redirect"] == "true"
    assert f"{NGINX}/affinity" not in annotations
    # Agent, terminal and preview WebSockets are long-lived.
    for key in ("proxy-read-timeout", "proxy-send-timeout"):
        assert int(annotations[f"{NGINX}/{key}"]) >= 3600
    hosts = {rule["host"] for rule in ingress["spec"]["rules"]}
    tls_hosts = {host for entry in ingress["spec"]["tls"] for host in entry["hosts"]}
    assert hosts <= tls_hosts


def test_ingress_routes_each_host_to_its_tier(manifests):
    app = _backends(_get(manifests, "Ingress", "enigma"))
    preview = _backends(_get(manifests, "Ingress", "enigma-preview"))
    assert set(app.values()) == {"enigma-api", "enigma-frontend"}
    # Previews get their own Ingress: the API's annotations (upload cap, retrying 503s) are
    # wrong for a tenant's dev server and for the proxy's self-retrying "Waking preview" 503.
    assert set(preview.values()) == {"enigma-preview-proxy"}
    assert all(host.startswith("*.") for host in preview)
    annotations = _get(manifests, "Ingress", "enigma-preview")["metadata"]["annotations"]
    assert "http_503" not in annotations.get(f"{NGINX}/proxy-next-upstream", "")


def _site(host: str) -> str:
    """Registrable domain, by the last two labels (enough for the placeholder hosts)."""
    return ".".join(host.lstrip("*.").split(".")[-2:])


def test_previews_are_cross_site_from_the_app(manifests):
    # Preview pages are tenant-controlled. On the app's own site, SameSite=Lax session cookies
    # ride along on their requests -- a preview could open the API's terminal WebSocket as the
    # visitor (cross-site WebSocket hijacking) or POST to it (CSRF). A separate registrable
    # domain makes every such request cross-site, so the browser drops the cookie.
    app_sites = {_site(h) for h in _backends(_get(manifests, "Ingress", "enigma"))}
    preview_sites = {_site(h) for h in _backends(_get(manifests, "Ingress", "enigma-preview"))}
    proxy_base = _config(manifests)["DAYTONA_PREVIEW_PROXY_BASE_URL"].split("://", 1)[1]
    assert preview_sites == {_site(proxy_base)}
    assert not app_sites & preview_sites
    origins = _config(manifests)["BACKEND_CORS_ORIGINS"].split(",")
    assert not {_site(o.split("://", 1)[1]) for o in origins} & preview_sites


def test_network_policies_default_deny(manifests):
    policy = _get(manifests, "NetworkPolicy", "default-deny")
    assert policy["spec"]["podSelector"] == {}
    assert set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}


def _egress_ports(policy: dict) -> set[int]:
    ports: set[int] = set()
    for rule in policy["spec"].get("egress", []):
        assert rule.get("ports"), f"{policy['metadata']['name']} allows egress on every port"
        ports |= {p["port"] for p in rule["ports"]}
    return ports


def test_network_policies_are_least_privilege(manifests):
    api = _get(manifests, "NetworkPolicy", "enigma-api")
    worker = _get(manifests, "NetworkPolicy", "enigma-worker")
    proxy = _get(manifests, "NetworkPolicy", "enigma-preview-proxy")
    frontend = _get(manifests, "NetworkPolicy", "enigma-frontend")

    # Postgres (direct or PgBouncer), Redis, DNS, HTTPS to Daytona / the LLM / GitHub, and
    # OTLP/HTTP traces to the collector.
    for policy in (api, worker):
        assert _egress_ports(policy) == {53, 5432, 6432, 6379, 443, 4318}
    # The proxy never talks to Redis.
    assert _egress_ports(proxy) == {53, 5432, 6432, 443}
    assert _egress_ports(frontend) == set()
    assert "Egress" in frontend["spec"]["policyTypes"]
    # Workers serve no traffic: their one ingress is the metrics scrape, from the Prometheus
    # pods only (never the ingress controller), on the port the container exposes.
    assert "Ingress" in worker["spec"]["policyTypes"]
    (scrape,) = worker["spec"]["ingress"]
    for peer in scrape["from"]:
        assert peer["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "prometheus"}
        assert peer["namespaceSelector"]["matchLabels"], "pinned to the monitoring namespace"
    worker_container = _container(_get(manifests, "Deployment", "enigma-worker"))
    (metrics_port,) = [p for p in worker_container["ports"] if p["name"] == "metrics"]
    assert {p["port"] for p in scrape["ports"]} == {metrics_port["containerPort"]}
    assert metrics_port["containerPort"] == _setting(
        manifests, worker_container, "METRICS_PORT", Settings.model_fields["metrics_port"].default
    )
    for policy in (api, proxy, frontend):
        (rule,) = policy["spec"]["ingress"]
        assert rule["from"][0]["namespaceSelector"]["matchLabels"], "ingress controller only"
        # Any other peer (the metrics scraper) is pinned to specific pods, not a namespace.
        for peer in rule["from"][1:]:
            assert peer["podSelector"]["matchLabels"], policy["metadata"]["name"]

    migrate = _get(manifests, "NetworkPolicy", "enigma-migrate")
    for policy in (api, worker, proxy, migrate):
        for rule in policy["spec"]["egress"]:
            ports = {p["port"] for p in rule["ports"]}
            # A port-only rule reaches every host on that port, internet and cluster alike:
            # an SSRF/exfiltration path around the HTTPS restriction below.
            assert rule.get("to"), f"{policy['metadata']['name']} egress {ports} has no `to:`"
            if ports & {5432, 6432, 6379}:
                # Managed Postgres/Redis over private networking; never the public internet.
                for peer in rule["to"]:
                    cidr = peer["ipBlock"]["cidr"]
                    assert cidr != "0.0.0.0/0" and "except" not in peer["ipBlock"], cidr
            if 53 in ports:
                # DNS only to the cluster resolver: no tunnelling out through port 53.
                (peer,) = rule["to"]
                assert peer["podSelector"]["matchLabels"] == {"k8s-app": "kube-dns"}
            if 4318 in ports:
                # Traces go to specific collector pods, never to an address range.
                (peer,) = rule["to"]
                assert peer["podSelector"]["matchLabels"] and peer["namespaceSelector"]
            if 443 in ports:
                # HTTPS is for the internet (Daytona, LLM, GitHub), not the cluster's own API
                # server, private ranges or cloud metadata.
                (peer,) = rule["to"]
                block = peer["ipBlock"]
                assert block["cidr"] == "0.0.0.0/0"
                assert {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"} <= set(
                    block["except"]
                )


# --- release ------------------------------------------------------------------------------


def test_migrations_run_as_a_pre_deploy_job(manifests):
    job = _get(manifests, "Job", "enigma-migrate")
    container = _container(job)
    pod = _pod_spec(job)
    assert container["args"] == ["alembic", "upgrade", "head"]
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert job["spec"]["backoffLimit"] <= 2
    assert job["spec"]["ttlSecondsAfterFinished"] > 0


def test_pre_deploy_selector_covers_what_the_migration_needs(manifests):
    # deploy/README.md step 1 applies `-l enigma/phase=pre-deploy` before anything rolls, so on a
    # first install the Job must not start without its namespace, config or egress policy.
    namespace = yaml.safe_load((PRODUCTION / "namespace.yaml").read_text())
    pre_deploy = [
        namespace,
        _get(manifests, "ConfigMap", "enigma-config"),
        _get(manifests, "Job", "enigma-migrate"),
        _get(manifests, "NetworkPolicy", "enigma-migrate"),
    ]
    for resource in pre_deploy:
        assert resource["metadata"]["labels"].get("enigma/phase") == "pre-deploy", resource["kind"]
    for workload in _of_kind(manifests, "Deployment"):
        assert "enigma/phase" not in workload["metadata"].get("labels", {})
