# Enigma — OpenSandbox as the Primary Runtime, Step 1: Hardening With Proof (Plan 7a)

> **For agentic workers:** Execute task-by-task with TDD (red → green → refactor). Tick the `- [x]` steps as they land.

**Goal:** Make `runtime="opensandbox"` safe enough to replace the custom Docker provider for self-hosting, and **prove it with a live test**: one tenant's sandbox cannot reach another's.

Plan 7b, the cutover, runs **only after** this plan's live security suite passes:
- OpenSandbox becomes the default self-host runtime;
- compose, k8s and docs are updated;
- Docker is deprecated.

Until then, Docker stays the self-host default.

**Spec:** `docs/superpowers/specs/2026-11-26-enigma-verifiable-agent-loop-design.md` §4.3 ("Keamanan wajib sebelum produksi"), §9.2. **Spike:** `docs/research/2026-11-26-opensandbox-spike.md`.

## Current state (verified)

**Enigma's adapter.** `services/sandbox/opensandbox.py` passes the provider conformance suite at fake level. But:
- `_create` is a bare `Sandbox.create("enigma-sandbox:latest", timeout=None)` (`:376-381`).
- It sets no resources, metadata, network policy or security check, and the image is a hardcoded placeholder.
- There is no idle stop, no orphan sweep scoped to this installation (`SandboxFilter()` is unscoped, `:430`), and no live integration or PTY test.
- Docker, by contrast, has `assert_safe_container_spec`, a live `CapEff` check, one network per sandbox, an enforced egress proxy, a disk quota and upkeep (AGENTS.md, Docker paragraph).

**opensandbox-server 1.2.1** (read in source; paths under `opensandbox_server/`):

*Security profile:*
- It is **server-wide config only** (`[docker]` in `sandbox.toml`, `config.py:1322-1428`).
  - `drop_capabilities` defaults to 9 caps, not ALL, but `["ALL"]` is accepted.
  - `no_new_privileges=true`, `pids_limit=4096`.
- There is no read-only rootfs and no forced user; the image's `USER` applies.
- **A create request can weaken it.** `extensions["bootstrap.execd.isolation"]` adds `SYS_ADMIN` and unconfined seccomp/AppArmor (`docker_service.py:940-962`), and a `pvc` volume can mount **any** named Docker volume (`volumes.py:230-330`). Enigma must never send either.

*Resources:*
- Set per request with `resource={"cpu","memory","disk"}` (`api/schema.py:120-142`).
- The SDK defaults to `cpu=1`, `memory=2Gi`; the live E2E sat at the 2 GiB limit.
- The server has no caps.

*Auth:*
- `server.api_key` guards the lifecycle API (header `OPEN-SANDBOX-API-KEY`).
- **`/sandboxes/{id}/proxy/{port}` bypasses auth in single-tenant mode** (`middleware/auth.py:80`, re-checked).
- `OPENSANDBOX_INSECURE_SERVER=YES` disables auth entirely.

*Network:*
- All sandboxes share one bridge, with **no inter-sandbox isolation**.
- Each sandbox's execd port (44772) is published on `docker.publish_host` (default `0.0.0.0`).
- The server never sets execd's `EXECD_ACCESS_TOKEN`, and execd with no token accepts every request (upstream `execd/pkg/web/router.go:239-285`). So **any sandbox on the bridge can exec, read files and open terminals in any other sandbox.**
- Egress policy (`networkPolicy`, FQDN/IP/CIDR rules, enforced by a sidecar, `dns` or `dns+nft` mode) is per request and opt-in.

*Images and lifetime:*
- There is no image allow-list or signature check.
- `image@sha256:…` passes through to Docker.
- A `null` timeout skips the TTL cap, and there is no orphan sweeper.

## Design

### 1. Spike first: can the cross-sandbox hole be closed?

The design of §4 depends on two unverified mechanisms, so Task 1 settles them live, against opensandbox-server 1.2.1 and execd `v1.1.0`, before anything else:

- **(a) execd token.** Does `env={"EXECD_ACCESS_TOKEN": t}` at create make execd refuse requests without `X-EXECD-ACCESS-TOKEN: t`? Do the SDK (`ConnectionConfig.headers`) and our `/pty` client then still work?
- **(b) Network deny.** Does a `networkPolicy` that allows public egress but denies the sandbox bridge subnet, RFC 1918, link-local and the host block a sandbox from reaching a sibling's execd **by IP**? Check this in `dns` and in `dns+nft` mode.

The outcome decides the **isolation strategy**:
- **(a) works:** tokens are the primary control, and (b) is defence in depth.
- **Only (b) works:** the network deny is required, and the server must run in `dns+nft`.
- **Neither works:** stop. Plan 7b is blocked, and we report back with the upstream issues to file. Docker stays the self-host default.

### 2. A safe create request (Enigma side, every sandbox)

`OpenSandboxRuntime._create(workspace)` builds the request through one function, `build_create_request(settings, workspace)`, and `assert_safe_create_request(request)` refuses anything outside an allow-list of fields:
- **No** `extensions`, `volumes`, `platform`, secure-access or `entrypoint` overrides.
- The image must equal `OPENSANDBOX_SANDBOX_IMAGE`.

The request contains:
- **Image:** `OPENSANDBOX_SANDBOX_IMAGE` (default `enigma-sandbox:latest`; accepts `name@sha256:…`). `OPENSANDBOX_REQUIRE_IMAGE_DIGEST=true`, the default when not in dev, refuses a tag.
- **Resources:** `OPENSANDBOX_SANDBOX_CPU` (default `2`), `OPENSANDBOX_SANDBOX_MEMORY` (default `4Gi`), and `OPENSANDBOX_SANDBOX_DISK` (empty means none, like `DOCKER_SANDBOX_DISK_MB`).
- **Metadata:** `enigma.installation=<sandbox_installation.id>` and `enigma.workspace=<id>`.
- **Execd token** (if 1a holds): `env={"EXECD_ACCESS_TOKEN": token}`, with `token = HMAC(AUTH_SECRET_KEY, "execd:" + workspace.id)`. It is derived, never stored, and sent on every SDK and `/pty` call.
- **Network policy:** from `SANDBOX_NETWORK_BLOCK_ALL` / `SANDBOX_NETWORK_ALLOW_LIST`, the same settings Docker and Daytona honour. Private, link-local, loopback and `OPENSANDBOX_SANDBOX_SUBNET` targets are **always** denied (per the spike).
- **Timeout:** `None` is kept, because Enigma owns the lifecycle. The upkeep in §4 replaces TTLs.

### 3. Verify the floor in the live sandbox

After create (and on adoption in `_get_sandbox`'s first connect per process), run one probe exec:
- `CapEff` must be 0 and `NoNewPrivs` must be 1.
- `id -u` must not be 0.
- If 1a holds, an unauthenticated request to its own execd from the backend must return 401.

A failure destroys the new sandbox and raises `SandboxRuntimeError("opensandbox sandbox failed its security check: <which>")`, the same pattern as Docker's `CapEff` adoption check. This catches a server whose `sandbox.toml` lacks `drop_capabilities=["ALL"]`.

### 4. Upkeep for OpenSandbox

`services/sandbox_maintenance.py` currently runs for Docker only. It is generalised to a per-provider adapter (`docker`, `opensandbox`):
- **Orphan sweep:** at startup and every pass, list by `metadata enigma.installation=<id>` (never unscoped), and destroy sandboxes no workspace row names.
- **Idle pause:** the `sandbox_activity` / `SANDBOX_AUTO_STOP_MINUTES` / busy-probe rules carry over (`/tmp` busy marker, live exec or terminal). Idle sandboxes are paused (`docker pause` server-side); `start()` resumes a paused sandbox (today a TODO, `:466-469`).
- **Gating:** `OPENSANDBOX_UPKEEP` (unset means on when the default runtime is opensandbox), as `DOCKER_UPKEEP` does.

### 5. Refuse an insecure server

`/health` and `choose_runtime` treat OpenSandbox as **unsafe** (not offered for new workspaces, reported `status: insecure`) when:
- `OPENSANDBOX_API_KEY` is empty and `OPENSANDBOX_ALLOW_INSECURE` (dev only) is not set; or
- an unauthenticated `GET /v1/sandboxes` against the server is **not** refused with 401.

The second is a cheap live probe per health pass, so a server started with `OPENSANDBOX_INSECURE_SERVER=YES` is caught.

### 6. Operator contract

`deploy/opensandbox/sandbox.toml` is the reference config, with every security-relevant key commented:
- `[server]`: `host` private, `api_key` from env.
- `[docker]`:
  - `network_mode="bridge"`, `publish_host` = a private IP (never `0.0.0.0`);
  - `drop_capabilities=["ALL"]`, `no_new_privileges=true`, `pids_limit`;
  - the egress mode the spike chose (§1).
- `[storage]`: `allowed_host_paths=[]`.
- `max_sandbox_timeout_seconds`.

`docs/self-host-opensandbox.md` covers the server, the socket risk (the server holds the Docker socket), its network placement (private, behind the backend, no TLS of its own), and the settings above.

### 7. The live security suite (the gate for Plan 7b)

`tests/test_opensandbox_integration.py` is opt-in (`OPENSANDBOX_INTEGRATION_TESTS=1`, a real server from the reference config) and runs weekly in CI like `docker-integration.yml`. It checks:
1. A sandbox is created with the configured resources, metadata and image. `CapEff=0`, `NoNewPrivs=1`, and the user is not root.
2. **Cross-tenant:** from sandbox A, sandbox B's execd (bridge IP:44772) and host-published ports are unreachable, or refuse without B's token.
3. Unauthenticated lifecycle API calls get 401. If 1a holds, unauthenticated execd calls also get 401.
4. Egress: with `SANDBOX_NETWORK_ALLOW_LIST=example.com`, the allowed host works, another host is refused, and private ranges are always refused.
5. PTY: create, type, disconnect, reconnect with the backlog replayed, resize, delete.
6. Upkeep: an orphan (metadata of this installation, no row) is removed. A sandbox of another installation is untouched. An idle sandbox is paused, then resumed by `start()`.
7. A create request with `extensions` or `volumes` is refused by `assert_safe_create_request`.

**Exit criterion for Plan 7b:** this suite is green against the reference config, and items 2–3 are green with the spike's isolation strategy.

## Global constraints

- No migration. The provider contract and the conformance suite stay unchanged (fakes updated, never weakened).
- The `opensandbox` SDK stays inside `services/sandbox/` (`test_boundary.py`).
- Secrets: the API key and execd tokens never appear in logs, `/health`, errors or evidence.
- Docker, Daytona and existing OpenSandbox workspaces keep working. Old sandboxes without metadata are adopted, not swept, until they are recreated (logged once).

## Review focus

- **Isolation:** no request field lets a tenant or a bug widen the profile. The token or network deny really blocks sibling access (proven live, not assumed).
- **No false sweep:** only sandboxes carrying this installation's id are ever destroyed.
- **Fail closed:** an insecure or misconfigured server is never offered for new workspaces.

---

### Task 1: Spike: execd token and network deny (live)

- [x] Script (scratchpad `spike7/spike.py`) against opensandbox-server 1.2.1 and execd v1.1.0. Server config: `drop_capabilities=["ALL"]`, `publish_host=127.0.0.1`, an API key. Sandboxes A (no token), B (token), C (egress policy: allow, deny RFC 1918 / CGNAT / link-local).
- [x] Results (identical in `dns` and `dns+nft` unless noted):

  | Check | Result |
  |---|---|
  | Floor (`drop_capabilities=["ALL"]`) | `CapEff=0`, `NoNewPrivs=1`, uid 1001 |
  | API without key | 401 (with key 200) |
  | **1a**: B's execd without the token (SDK, host port, from sibling A) | **401** each; with the token 200 |
  | A (no token) reached from B | **200**: the hole is real wherever a token is missing |
  | **1b**: C → A's execd by IP, `dns` mode | **200**: not blocked |
  | **1b**: C → A's execd by IP, `dns+nft` mode | **timeout: blocked** |
  | C → public HTTPS | 200 in both |
  | Server `/sandboxes/{id}/proxy/{port}` | timed out with and without the key here (inconclusive). Enigma does not use it; the server must not be reachable by tenants anyway (§6). |

- [x] **Strategy:** 1a and 1b, both required.
  - **Primary control:** every sandbox gets an execd token (1a).
  - **Defence in depth:** the server runs egress `dns+nft`, and every sandbox's policy denies private ranges (1b). This also keeps sandboxes off the host's and the server's ports.
- **Design consequence:** execd's port proxy (`/proxy/<port>`, which `get_endpoint` returns and previews and observations fetch) runs after the token middleware (`execd/pkg/web/router.go:64`). So the preview proxy and the observation fetch must send `X-EXECD-ACCESS-TOKEN`. `RuntimePreviewLink` gets a provider header (as Daytona's `X-Daytona-*`); Task 2 covers it, and the live suite checks a preview through the token.

### Task 2: Safe create request

**Files:** `services/sandbox/opensandbox.py` (`build_create_request`, `assert_safe_create_request`), `core/config.py` (`OPENSANDBOX_SANDBOX_*`, `OPENSANDBOX_REQUIRE_IMAGE_DIGEST`, `OPENSANDBOX_SANDBOX_SUBNET`), tests.

- [x] Failing tests (`tests/sandbox/test_opensandbox_hardening.py`):
  - image, resources, metadata and policy built from settings
  - digest required when configured
  - forbidden fields refused
  - the network policy always denies private ranges
  - the execd token is derived, sent and never logged
  - the token header reaches every SDK connection, PTY call and preview link
  - the preview proxy and the observation fetch forward it
- [x] Implement, green; conformance and boundary stay green.
  - `RuntimePreviewLink.headers` carries provider headers next to Daytona's renewable `token`.
  - The installation-id loader is shared with Docker (`services/sandbox/installation.py`).
  - Create uses `ready_timeout=120s`, because a sandbox with an egress sidecar takes longer than the SDK's 30 s default.
- [x] Security review fixes (no critical or high findings):
  - **Dedicated key:** the token is keyed by its own `OPENSANDBOX_EXECD_TOKEN_KEY`, required outside `local` (`insecure_setting_names`). Rotating `AUTH_SECRET_KEY` no longer locks Enigma out of sandboxes. Rotating the execd key does, until they are recreated (documented).
  - **Observation fetch:** it follows redirects by hand and sends provider headers only to the preview's own origin.
  - **Egress:** `::ffff:0:0/96` (IPv4-mapped) is always denied. Non-CIDR allow-list entries (hostnames) are dropped, as on the other providers.
  - **Preview proxy:** it strips a visitor-supplied `X-EXECD-ACCESS-TOKEN`.

### Task 3: Live floor check

**Files:** `opensandbox.py`, tests.

- [x] Failing tests:
  - a probe failure destroys the new sandbox and raises naming the check (CapEff, NoNewPrivs, root, an empty probe)
  - adoption checks once per process, and refuses but keeps a sandbox below the floor
- [x] Implement, green (`FLOOR_PROBE`, shell builtins and `/usr/bin/id`).
- [x] Security review of the Task 2 commit (automated), both fixed:
  - **Critical: preview path traversal.** OpenSandbox's preview URL shares an origin with execd's control API, and the proxy sends the execd token. A visitor path with dot segments (`..`, `%2e%2e`, double-encoded, backslashes) could climb out of `/proxy/<port>` and run commands. `check_preview_path` now refuses such paths at the HTTP and WebSocket entry (400 / policy close), and `_upstream_url` re-checks that the built path stays under its prefix.
  - **Medium: observation SSRF.** The fetch follows redirects only within the preview's origin; a redirect elsewhere is observed as the redirect itself, never fetched.

### Task 4: Refuse an insecure server

**Files:** `services/sandbox/availability.py`, `api/health.py`, `opensandbox.py` (`auth_probe`), tests.

- [x] Failing tests:
  - no key → `insecure`, not offered
  - a server answering 200 unauthenticated → `insecure`
  - `OPENSANDBOX_ALLOW_INSECURE` → offered with a warning, refused outside `local`
  - the key never appears in the reason
- [x] Implement, green:
  - `InsecureRuntimeError` from `health()`, mapped to `status: insecure` in `check_runtime`;
  - `/health` counts it as degraded;
  - the probe is an unauthenticated `GET /v1/sandboxes`, which must be 401.
- [x] Automated review of the Task 3 commit: teardown (`delete`, `kill_pty`) skips the floor check, so a sandbox that fails it can always be removed.

### Task 5: Upkeep for OpenSandbox

**Files:** `services/sandbox_maintenance.py`, `opensandbox.py` (`list_managed`, `pause`, resume in `start`, activity touches), `main.py`/`worker.py` wiring, tests.

- [x] Failing tests (`tests/test_opensandbox_maintenance.py`):
  - scoped orphan sweep; unlabelled sandboxes are never touched
  - another installation untouched (listing filtered by its metadata, and re-checked)
  - idle pause leaves busy sandboxes
  - `start()` and any use resume a paused sandbox (once per minute per process)
  - unscoped listing never used
  - execs, streams and terminal output count as activity; a long command keeps a heartbeat
  - the loop starts only where it is wanted
- [x] Implement, green:
  - **Busy rule:** the `sandbox_activity` heartbeat (every 60 s while an exec runs; terminal output as it flows), which is shared across processes through Postgres. Docker's in-sandbox busy probe needs its own exec wrapper, which OpenSandbox does not have.
  - **Loop:** `start_opensandbox_maintenance` runs next to Docker's, from `main.py` (inline) and `worker.py` (queue), and is gated by `OPENSANDBOX_UPKEEP` (unset means on when the default runtime is opensandbox).
  - **Not done:** lost-sandbox detection. Sandboxes created before labels are not listed, so a missing listing proves nothing.

### Task 6: Reference config, docs

- [x] `deploy/opensandbox/sandbox.toml`, `docs/self-host-opensandbox.md`, `.env.example` (`OPENSANDBOX_*`).
- [x] AGENTS.md note 32.

### Task 7: Live security suite

**Files:** `tests/test_opensandbox_integration.py`, `.github/workflows/opensandbox-integration.yml` (weekly, opt-in).

- [x] Items 1–7 of §7, run locally (2026-11-27) against opensandbox-server 1.2.1 started from `deploy/opensandbox/sandbox.toml` (only the store path changed), with execd v1.1.0 and egress v1.1.7. **9/9 passed in 112 s**, and no sandbox was left behind:

  | # | Check | Result |
  |---|---|---|
  | 1 | Floor (CapEff 0, NoNewPrivs 1, non-root), `memory.max` = 2Gi, installation/workspace metadata | pass |
  | 2 | Sandbox A → sandbox B's execd by IP | not 200 (blocked) |
  | 3 | Lifecycle API without the key / execd without the token / with the token | 401 / 401 / 200 |
  | 3 | A preview without / with the token | 401 / 200 |
  | 4 | Egress: public open; `10.0.0.1`, `169.254.169.254`, `192.168.1.1` blocked; allow-list `1.1.1.1/32` allows 1.1.1.1 and blocks 8.8.8.8 | pass |
  | 5 | Terminal: type, resize, disconnect, reconnect to the same shell (its state survives; output resumes from the last offset) | pass |
  | 6 | Orphan sweep removes this installation's orphan and leaves another installation's; pause, then the next use resumes | pass |
  | 7 | `extensions` / `volumes` refused | pass |

- [x] `.github/workflows/opensandbox-integration.yml`: weekly or manual. It builds the image, starts the server from the reference config with a masked random key, runs the suite, and checks no sandbox is left.
- **Exit criterion for Plan 7b met** locally: the suite is green against the reference config, and items 2–3 hold with the chosen strategy (token + `dns+nft`). The weekly workflow is the standing check.
- Note: the suite surfaced no provider bug. The two first-run failures were test assumptions: a bare sandbox has no `workspace` dir yet, and a reconnect resumes from the last offset rather than replaying the whole backlog.

## Out of scope (Plan 7b and later)

- **Plan 7b, cutover:**
  - `SANDBOX_RUNTIME_DEFAULT=opensandbox` for self-host;
  - `docker-compose.opensandbox.yml` (server + reference config);
  - k8s (server placement, network policy);
  - Docker marked deprecated (still selectable), then removed in a later release;
  - frontend copy.
- gVisor/Kata (`secure_runtime`), as an optional operator hardening once the basics are in.
- Pushing the upstream gaps (proxy auth bypass, per-sandbox networks, server-side forced profile) to the OpenSandbox project.
