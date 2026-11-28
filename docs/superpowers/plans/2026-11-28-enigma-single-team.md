# Enigma — Single Team, Self-Hosted: One Execution Mode (Plan 13, Track D)

> **For agentic workers:** Every step below is one PR and can be executed cold: read its context brief, do its tasks, run its verification, meet its exit criteria. Tick the `- [x]` boxes as they land. This track only removes. **Run it before Track B** (`2026-11-28-enigma-loop-additions.md`): once D1 and D2 are merged, every queue-mode clause in Plans 11 and 12 no longer applies. The cross-track map is spec §14.

**Goal:** Enigma runs as one backend process for one team. There is one way a run executes.

**Spec:** `docs/superpowers/specs/2026-11-26-enigma-verifiable-agent-loop-design.md` §9.5 (decided 2026-11-28: self-hosted, single team).

**Why this is worth more than any feature cut:** two execution modes mean every new piece of the loop has to be correct twice (job retries, leases, a reaper, cross-process events). Most critical findings in the reviews of Plans 11 and 12 came from that. Nothing a single team on one host does needs it.

## Decisions (2026-11-28)

| Area | Decision | Why |
|---|---|---|
| Queue mode (arq worker, jobs, leases, reaper, Redis event bus) | **Removed** | One team, one host, one backend process |
| Kubernetes worker Deployment and KEDA autoscaling | **Removed** | They exist only to run and scale the worker |
| Kubernetes API | **One replica, `Recreate`, no autoscaler** | The API now executes the runs, and the startup sweep assumes a single backend: a second live replica would end the first one's runs |
| A deploy while runs are active | The API waits `SHUTDOWN_RUN_GRACE_SECONDS=300` for them, then they end `interrupted_by_restart` | The worker waited 7 500 s so a rollout never interrupted a run. With one replica and `Recreate` that would be up to two hours of downtime per deploy. A single team deploys when idle; five minutes covers a run that is finishing. Change the number if you disagree — it is one setting |
| `agent_run_timeout_seconds` | **Removed** | Only the worker enforced it; inline runs have never had a time limit. Removing it keeps today's inline behaviour. A time limit for inline runs is a possible follow-up, not part of a removal track |
| Tracing spans for runs | Unchanged for inline (there are none) | `run_span` and the row `traceparent` reader exist only in the worker; inline runs get no span today |
| Redis | **Stays** | The rate limiter uses it, and presence uses its pub/sub until Track A step A1 removes presence. It stops being a job queue and an event bus. Bot-to-bot communication (Track C) is rows in Postgres and never needed Redis |
| Kubernetes base manifests (API, preview proxy, frontend, migrate job) | Stay | They do not tax any other step; removing them is a separate decision |
| Project → Team → Workspace | **Stays in the data model**; made invisible for a single team (D4) | Non-owner access to a workspace *is* team membership (`services/access.py`), and budgets are scoped to teams. Flattening is a data migration and a new access model, for a benefit a default project and team give almost for free |
| Run caps, retention, tracing, PgBouncer option, stateless preview proxy | Stay | Off by default or harmless |

## Baseline (verified 2026-11-28)

- **The Kubernetes config runs queue mode today:** `deploy/k8s/base/config.yaml:18` sets `RUN_EXECUTION_MODE: queue`, and `tests/test_deployment.py` (~502–506) asserts it. Refusing that value at startup before the manifests change would crash-loop the deployment. This fixes the step order (see the step map).
- **Two queue test files are the suite's shared fixture modules.** There is no `tests/conftest.py`. `tests/test_job_queue.py` and `tests/test_worker_jobs.py` export `db_session`, `make_run`, `make_user_and_workspace`, `seed`, `queue_settings`, `FakeDispatcher`, `FakeDaytona`, `seed_run`, `seed_command_run`, `make_ctx`, imported at module level by `test_retention.py`, `test_event_streaming.py`, `test_fleet_metrics.py`, `test_run_capacity.py`, `test_metrics_runs.py`, `test_tracing.py`, `test_correlation.py`, `test_verification_hardening.py`. Deleting them first breaks collection of all of these.
- **`services/jobs.py` has ten importers, not four:** the four start sites, plus `app/main.py` (~26–29, ~109–114, ~147: `close_run_dispatcher`, the `RunQueueUnavailableError` handler, a `RedisEventBus` isinstance check), `api/specs.py` (~31, ~341), `services/verification/fixer.py` (~28, ~75, ~85), `services/verification/spec_link.py` (~22), and a `dispatcher` constructor parameter in the agent, command, Spec Kit and verification-execution services. `services/reaper.py` imports `arq` and `jobs` (~28, ~37), so it cannot outlive them.
- **Queue-only methods inside services that stay:** `claim_run`, `renew_lease`, `execute_claimed_run`, `_enqueue_run`, and the `lease_expired` / `unclaimed` reasons of `fail_active_run` (`services/agents.py` ~386–520, `services/commands.py` ~465–565).
- **Queue state written on inline paths:** `stop_active_run` always calls `publish_control` (`agents.py:749`) though only a worker listens; `_transition` sets `lease_expires_at=None` (`agents.py:768`), as do `reconciliation.py` (~99, ~115) and `commands.py` (~769, ~779).
- **The fleet sampler runs inline** (`main.py:86`) and calls `waiting_run_counts` (`services/fleet_metrics.py` ~35, ~112); queue metrics live in `core/metrics.py` (~36–105, ~148, ~228).
- **Kubernetes API today:** an HPA with `minReplicas: 2`, `maxReplicas: 10` (`deploy/k8s/base/api.yaml` ~116–146), `terminationGracePeriodSeconds: 45`, `SHUTDOWN_RUN_GRACE_SECONDS: "15"` (`config.yaml:32`); the worker had 7 620 s. `test_deployment.py` requires RollingUpdate with `maxUnavailable == 0` for served tiers (~531–537) and an API HPA with `minReplicas >= 2` (~638–649), and reads `Settings.model_fields` for `worker_job_timeout_seconds`, `worker_max_jobs`, `metrics_port` (~602, ~674, ~784).
- `docker-compose.opensandbox.yml` (~75–80) overrides the `worker` service; compose tests in `test_deployment.py` iterate a `worker` service (~172, ~191, ~240, ~290, ~434); `overlays/opensandbox-external/network-policy.yaml:22` lists `worker` (asserted ~418).
- `agent_run_timeout_seconds` (`core/config.py:71`, `.env.example:47`) is read only by `app/worker.py`.
- **The surviving tests run on the queue harness.** `queue_settings()` in `tests/test_job_queue.py` (~25–27) sets `run_execution_mode="queue"`, so a test can create a run without it executing; `Settings` ignores unknown fields (`core/config.py:309`), so once the field is gone those callers silently get inline settings and `create_run` starts a real task. Several surviving tests also drive runs through `app.worker.run_agent_job(make_ctx(...))` (`test_metrics_runs.py`, `test_correlation.py`, `test_tracing.py`).
- **Files slated for deletion hold the only tests of behaviour that must not change:** `test_run_leases.py` ~111–225 (stop is idempotent, `stop_active_run` needs no user, a late completion cannot overwrite a stop) and ~719, ~734 (the `create_run` race and its advisory lock); `test_job_queue.py` ~121–188 (the one-active-run index); `test_worker_jobs.py` ~594, ~613 (stream `catch_up`); `test_verification_jobs.py` ~51 (inline verification start).
- **Two `run_execution_mode` readers run on every start:** `services/runtime.py:317` at import (`event_bus = _default_event_bus()`, line 329) and `services/capacity.py:99` inside every `create_run`.
- **The inline path reads `enqueued_at`:** `observe_run_start("agent", run.enqueued_at, …)` (`agents.py` ~1072) and the same for commands (`commands.py:742`); the metric is `run_start_latency_seconds` (`core/metrics.py` ~233).
- **`FixDeferredError` is not queue backpressure.** It is raised when the app is draining (`fixer.py` ~129–131), consumed inline (`orchestrator.py` ~310) and tested. Only `RunQueueUnavailableError` in the fixer is queue-only.
- Queue-only columns also include `attempt` and `heartbeat_at` (`models/agent.py` ~145–148, `models/command.py` ~186–188).
- **Worker vs API pod today:** worker requests 500m / 1Gi, limit 2Gi, `/tmp` 1Gi (`worker.yaml` ~70–87); API 250m / 512Mi, limit 1Gi, `/tmp` 256Mi (`api.yaml` ~80–97). Their egress rules and `envFrom` are the same, and `/metrics` is already on the API's port.
- **`kubectl apply -k` does not prune** (`deploy/README.md` ~109–113): objects removed from the manifests stay in an existing cluster.
- Other test-to-test imports beyond the two fixture files: `test_metrics_runs.py:14` imports from `tests.test_run_leases`; `test_fleet_metrics.py:28` from `tests.test_run_capacity`; verification tests import from each other.
- `run_execution_mode` (`core/config.py:62`, default `inline`) is read in ten places: `app/worker.py:408`; `app/main.py:71,76,82` (what the lifespan starts); `services/capacity.py:99` (queue depth); `services/runtime.py:317` (which event bus); and the four places a run is started — `services/agents.py:377`, `services/commands.py:451`, `services/speckit.py:445`, `services/verification/execution.py:59`.
- Queue-only modules: `app/worker.py` (507 lines), `app/worker_metrics.py` (148), `services/jobs.py` (166), `services/leases.py` (55), `services/reaper.py` (271: `reap_expired_leases`, `reap_lost_jobs`, `reap_lost_verification_jobs`, `run_reaper`). Inline runs have no lease and are never reaped (AGENTS.md gotcha 21).
- `services/runtime.py` holds both the in-process pieces that stay (`InMemoryRunRegistry`, `InMemoryEventBus`, `InMemoryRunLock`) and `RedisEventBus` (~144–290), chosen by `_default_event_bus` (~315).
- Queue settings: `run_queue_name`, `worker_id`, `worker_max_jobs`, `worker_max_tries`, `worker_job_timeout_seconds`, `run_lease_seconds`, `run_heartbeat_seconds`, `run_reaper_interval_seconds`, `run_unclaimed_grace_seconds`, `run_queue_max_depth`, `metrics_port` (worker).
- Queue columns on `agent_runs` and `command_runs`: `enqueued_at`, `claimed_at`, `worker_id`, `lease_expires_at` (`models/agent.py` ~140–149, `models/command.py` ~183–189).
- Removed settings already have a pattern: `REMOVED_SETTING_NAMES` (`core/config.py:524`) logs a leftover name once, and the Docker removal made an unsupported value fail startup with a message.
- Compose: the `worker` service is behind `profiles: ["queue"]`; `RUN_EXECUTION_MODE` is forwarded to the backend (`docker-compose.yml` ~105, ~128–155).
- Kubernetes: `deploy/k8s/base/worker.yaml` (152 lines) plus KEDA; `tests/test_deployment.py` has 43 tests, 39 lines of which mention the worker, the queue or KEDA; CI renders and validates the manifests including KEDA CRDs (`.github/workflows/ci.yml` ~110–121).
- Tests that exist only for queue mode: `test_job_queue.py`, `test_worker_jobs.py`, `test_worker_metrics.py`, `test_run_leases.py`, `test_redis_event_bus.py`, `test_verification_jobs.py`. Tests that cover both modes and must be trimmed, not deleted: `test_run_capacity.py`, `test_reconciliation.py`, `test_event_streaming.py`, `test_lifecycle.py` (~338–360), `test_metrics_runs.py`, `test_fleet_metrics.py`, `test_data_layer.py`, `test_run_runtime.py`, `test_tracing.py` and `test_correlation.py` (both import `app.worker` at module level), `test_retention.py` (~428–454), `test_verification_fixer.py`, `test_verification_hardening.py` (~202–298), `test_verification_auto_verify.py`, `test_deployment.py`. This list came from two passes and may still be short: **grep for every deleted module name in `backend/tests` before deleting it.**
- Also mentions queue mode: `README.md` (~371, ~428), `.env.example` (~40–55, ~81–82), `deploy/README.md`, and the frontend constant `INTERRUPTED_WORKER_DIED` (`frontend/src/lib/api.ts` ~428, ~438) with its tests.
- Redis users that are **not** queue mode and stay: `services/rate_limit.py`, `api/rate_limits.py`, `services/redis.py`, `api/health.py`. Check `api/agents.py`, `api/commands.py` and `services/fleet_metrics.py` when you get there: each imports Redis today, and D2 must say for each whether that use was queue-only.
- Migration rules: expand-only; a drop needs a `# contract:` line and ships one release after the code stopped using the column (AGENTS.md gotcha 25).

## Invariants (run after every step)

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && pnpm lint && pnpm test && pnpm build
```

- If `pnpm` fails with a workspace error locally, run the binaries in `./node_modules/.bin/` directly.
- Inline behaviour does not change in any step: same statuses, same events, same stop behaviour, same startup sweep (`services/reconciliation.py`), same graceful shutdown.
- `tests/test_run_runtime.py` (no module-level in-flight state), `tests/test_data_layer.py` (no session held across a WebSocket), `tests/test_migration_safety.py` and `tests/sandbox/test_boundary.py` keep their rules. Remove a test case only when the thing it tests is gone.
- Each step updates `AGENTS.md` (gotchas 16, 17, 19–23, 25–27, 30 name queue mode, workers, leases or the reaper).

## Step map

**Order: D0 → D3 → D1 → D2. D4 at any time.** The ids are kept stable because the other plans refer to "D1 and D2" as "queue mode is gone"; the order is what the code forces.

| Order | Step | Does | Depends on | Model tier |
|---|---|---|---|---|
| 1 | D0 | Moves shared test fixtures out of the queue test files | — | default |
| 2 | D3 | Kubernetes: API runs inline as one replica; worker and KEDA removed | — | strongest |
| 3 | D1 | Ports tests to an inline harness, then removes the queue execution path: worker, jobs, reaper, leases, dispatcher, compose worker | D0, D3 | strongest |
| 4 | D2 | Removes what is left: Redis event bus, queue metrics, settings, queue-state writes and attributes | D1 | strongest |
| any | D4 | A single team never sees the hierarchy | — | default |
| later | (A10) | Drops the queue columns | D2, one release later | — |

Why this order: D3 must precede D1 because the manifests ask for queue mode and D1 turns that into a startup failure. D3 must precede D2 because `test_deployment.py` reads worker settings from `Settings.model_fields` and D2 deletes those settings. D0 must precede D1 because D1 deletes the files the fixtures live in.

Rollback: revert the PR. No step changes the schema. Reverting D3 alone after D1 has shipped would crash-loop the cluster; revert in reverse order.

---

## D0 — Move the shared test fixtures

**Context.** Two test files that will be deleted are also the fixture library for the suite (see Baseline). This step moves the fixtures and changes nothing else, so the deletions that follow are only deletions.

- [ ] Create `backend/tests/run_fixtures.py` with the fixtures and helpers other files import from `tests/test_job_queue.py` and `tests/test_worker_jobs.py`. Find the real list with `grep -rn "from tests.test_" backend/tests` and move what is imported from a file this track deletes: `test_job_queue.py`, `test_worker_jobs.py`, `test_run_leases.py` (`test_metrics_runs.py` imports `reap, runtimes` from it), `test_verification_jobs.py`, `test_worker_metrics.py`, `test_redis_event_bus.py`. Imports between files that stay (e.g. from `test_run_capacity`, `test_verification_orchestrator`) are not this step's business. Fixtures are already imported by name into each module (precedent: `tests/upkeep_fixtures.py`); keep that style, with `__all__` or `# noqa: F401` where ruff needs it.
- [ ] Helpers that exist only to fake the queue (`FakeDispatcher`, `queue_settings`, `make_ctx`) move too if a surviving test still uses them today; D1 removes them when their last user goes.
- [ ] Repoint every import. The two source files import from the new module too, so nothing is defined twice.
- [ ] No test is added, removed or changed in what it asserts.

**Verify.** Invariants; the number of collected and passed tests is the same before and after.

**Exit.** No surviving test file imports anything from a test file this track deletes.

## D3 — Kubernetes: one API replica, no worker, no KEDA

**Context.** In the cluster, runs execute in a worker fleet scaled by KEDA, and the API is two or more stateless replicas behind an autoscaler. After this step the API executes runs itself (inline mode, which the code already supports), as a single replica. This is a real change in how the cluster behaves, made before the code stops supporting the old way.

- [ ] `deploy/k8s/base/config.yaml`: `RUN_EXECUTION_MODE: inline` (line 18); remove the worker-only settings (~34, ~36); `SHUTDOWN_RUN_GRACE_SECONDS: "300"`.
- [ ] `deploy/k8s/base/api.yaml`: delete the HPA (~116–146); `replicas: 1`; `strategy: Recreate`; remove the PodDisruptionBudget and topology spread that only make sense for several replicas; **raise resources to at least the worker's** (requests 500m / 1Gi, limit 2Gi, `/tmp` 1Gi) — the API now executes the runs, and an OOM kill ends every one of them; `terminationGracePeriodSeconds` above the new run grace plus the drain delay, following the relation `test_deployment.py` already checks for grace periods against `SHUTDOWN_*`.
- [ ] Delete `deploy/k8s/base/worker.yaml` and its entry in `kustomization.yaml`; remove the KEDA objects, the worker's NetworkPolicy rules and metrics ingress from the base and from both overlays, including `overlays/opensandbox-external/network-policy.yaml:22`.
- [ ] `tests/test_deployment.py`:
  - remove the worker, queue and KEDA tests, including the one comparing the KEDA query with `services/capacity.py`, and the `RUN_EXECUTION_MODE: queue` assertion (~502–506);
  - **trim, do not delete**, tests that also guard tiers that stay: drop the worker from `WORKLOADS` (~106, which feeds `test_workloads_are_locked_down` and the writable-`/tmp` test ~613); in `test_network_policies_are_least_privilege` (~759–821) remove only the worker lines (~761, ~767, ~772–785, ~793) — it also guards the API, proxy, frontend and migrate policies; the two other reads of worker fields from `Settings.model_fields` (~602, ~674) go with their worker tests; the overlay assertion (~418) is an exact list — change it to `["api", "preview-proxy"]`;
  - `test_served_tiers_roll_without_losing_capacity` (~531–551) checks strategy **and** the `/ready`, `/live` and startup probes: split it, so the API leaves the strategy check and keeps the probe checks. Take `enigma-api` out of `test_stateless_tiers_autoscale_on_cpu`, `test_disruption_budgets_keep_a_replica_up` (~620) and `test_served_tiers_spread_across_nodes` (~629). The preview proxy and the frontend stay in all of them;
  - add a guard: the API Deployment has `replicas: 1`, strategy `Recreate`, and no HPA targets it. That rule was a comment in gotcha 17; now it is the only thing between a well-meaning `replicas: 2` and runs that kill each other.
  - leave the compose tests that iterate `worker` alone: the compose worker goes in D1.
- [ ] CI (`.github/workflows/ci.yml` ~121–127): drop the KEDA CRD `-schema-location` and rename the step; keep rendering and validating both overlays.
- [ ] **Upgrade notes for an existing cluster** in `deploy/README.md`, because `kubectl apply -k` does not prune: (1) wait until no run is active — the new API's startup sweep ends every active run, including ones a draining worker is still executing; (2) `kubectl delete` the objects this step removed, by name: the API HPA (left in place it scales the inline API back to two replicas, the exact hazard this step removes), the worker Deployment, its ScaledObject and TriggerAuthentication, the API PodDisruptionBudget, the worker NetworkPolicy; (3) apply.
- [ ] `deploy/README.md`, `docs/horizontal-scale/08-…`: one API replica; **a deploy waits up to five minutes for active runs and then ends them as `interrupted_by_restart`; deploy when idle**; the API is unavailable for the length of the restart.
- [ ] `AGENTS.md`: gotchas 17 and 25.

**Verify.** Invariants; `kubectl kustomize deploy/k8s/overlays/production` and `…/opensandbox-external` render with no worker, no KEDA object and no API HPA.

**Exit.** The manifests describe one API that runs the work, one preview proxy, one frontend; the code still supports both modes.

## D1 — Remove the queue execution path

**Context.** A run can start inline (a task in the backend process, tracked by the run registry) or be enqueued for an arq worker. This step removes the second path and everything that imports `arq` or `services/jobs.py`. After it, `RUN_EXECUTION_MODE=queue` is refused at startup. D0 and D3 are merged.

- [ ] **Test harness first, in this PR, before any deletion** (see Baseline: the surviving tests stand on the queue harness). In `tests/run_fixtures.py` add an inline harness: settings with the mock provider, and a way to create a run without it racing the test (a helper that awaits the registered task, or a patched `_execute_run`). Port to it: the stop-idempotence, stop-without-user and late-completion cases and the `create_run` race cases from `test_run_leases.py`; the one-active-run index cases from `test_job_queue.py`; the stream `catch_up` cases from `test_worker_jobs.py`; the inline start case from `test_verification_jobs.py`; and the cap, race, correlation, tracing and metrics cases that today call `queue_settings()` or `run_agent_job(make_ctx(...))`. Only then delete. A case is deleted only when it is *named for* claim, lease, reaper, enqueue, redelivery or worker.
- [ ] Start sites (`agents.py:377`, `commands.py:451`, `speckit.py:445`, `verification/execution.py:59`): delete the queue branch, keep the inline one unchanged. Remove the `dispatcher` constructor parameter from those four services and from every caller.
- [ ] The other importers of `services/jobs.py`: `app/main.py` (`close_run_dispatcher`, the `RunQueueUnavailableError` handler; leave the `RedisEventBus` isinstance check for D2 only if it still compiles, otherwise remove it here), `api/specs.py` (~31, ~341), `services/verification/fixer.py` (~28, ~75, ~85), `services/verification/spec_link.py` (~22). For the fixer: remove only `RunQueueUnavailableError`. **Keep `FixDeferredError` whole**: it is raised when the app is draining, consumed inline by the orchestrator, and tested.
- [ ] Queue-only methods: delete `claim_run`, `renew_lease`, `execute_claimed_run`, `_enqueue_run` and the `lease_expired` / `unclaimed` reasons of `fail_active_run` from `services/agents.py` and `services/commands.py`.
- [ ] `app/main.py` (~71–82): the lifespan starts what it starts today in inline mode, unconditionally.
- [ ] The two remaining readers of the setting, which run on every start: `services/runtime.py` — `_default_event_bus` returns `InMemoryEventBus()` (the `RedisEventBus` class stays for D2, unused); `services/capacity.py` — delete `enforce_queue_depth` and its two calls (`agents.py:326`, `commands.py:420`); `waiting_run_counts` stays for D2 because the fleet sampler still calls it.
- [ ] Delete `app/worker.py`, `app/worker_metrics.py`, `services/jobs.py`, **`services/reaper.py`** (it imports `arq` and `jobs`; inline runs were never reaped) and **`services/leases.py`** (after these deletions nothing imports it).
- [ ] Tests: delete `test_job_queue.py`, `test_worker_jobs.py`, `test_worker_metrics.py`, `test_verification_jobs.py`, `test_run_leases.py` (it imports `jobs` and `app.worker`). In the both-mode files remove the queue, worker, reaper and lost-job cases only — including the `app.worker` imports at the top of `test_tracing.py` and `test_correlation.py`, the worker cases in `test_metrics_runs.py`, and the lost-job cases in `test_verification_hardening.py`. Remove the queue-faking helpers from `tests/run_fixtures.py` once nothing uses them.
- [ ] Settings: remove `run_execution_mode` and `agent_run_timeout_seconds` (see Decisions). A leftover `RUN_EXECUTION_MODE=inline` is ignored and logged once through `REMOVED_SETTING_NAMES`; `RUN_EXECUTION_MODE=queue` **fails startup** with a message naming this plan and the last release with a worker. Silently running inline for someone who configured a worker fleet would be worse than refusing.
- [ ] Compose: remove the `worker` service, its `queue` profile, `worker_venv` and the `RUN_EXECUTION_MODE` forward from `docker-compose.yml`, **and the `worker` override in `docker-compose.opensandbox.yml`** (~75–80; an override of a missing service is an error). Update the compose tests in `test_deployment.py` that iterate `worker`.
- [ ] Remove `arq` from `backend/pyproject.toml` and re-lock (`uv lock`). Leave the `redis<6` pin for D2.
- [ ] Docs: `AGENTS.md` (the `arq` command, gotchas 19, 27, 30 — the reaper sentences — and gotcha 26's worker half: say plainly that inline runs get no trace span); `README.md`, `.env.example`, `deploy/README.md`; `docs/horizontal-scale/02-…` gets a first line saying it describes a removed mode.
- [ ] Frontend: **keep** `INTERRUPTED_WORKER_DIED` and its label. No new run can get that reason, but stored runs carry it and the history view must still explain them.
- [ ] `deploy/k8s/base/config.yaml`: remove the `RUN_EXECUTION_MODE: inline` line D3 wrote, or every pod logs `removed_setting_name` on start.
- [ ] Leave for D2: the `RedisEventBus` class, `waiting_run_counts`, the queue metrics, the lease and worker settings, the queue-state writes and columns.

**Verify.** Invariants; `grep -rn "run_execution_mode\|\barq\b\|services.jobs\|app.worker" backend/app backend/tests` is empty; `podman compose up --build` and the OpenSandbox variant both start with no worker; one agent run, one command run, one Spec Kit phase and one verification run each complete.

**Exit.** There is one way a run executes; a configuration that asks for the other is told so at startup; the tree is green without D2.

## D2 — Remove what only queue mode needed

**Context.** With no worker, nothing claims a run, holds a lease, or publishes events to another process. This step removes that machinery from code that otherwise stays. It is the careful one.

- [ ] Inventory first, in the PR: for each of `RedisEventBus`, `capacity.waiting_run_counts`, `publish_control` / `watch_control`, `get_redis_client(subscriber=True)` (presence uses it until Track A step A1; do not remove the option while presence exists), and the Redis imports in `api/agents.py`, `api/commands.py` and `services/fleet_metrics.py` — who calls it now? Remove what has no caller; keep what an inline path still uses and say which.
- [ ] Stop path: `stop_active_run` stops publishing to the control channel (`agents.py:749`); `publish_control` and `watch_control` go. Stop is the in-process stop event.
- [ ] `AgentService._transition` **keeps its conditional `WHERE`** — that is what makes a stop and a completion race safely in one process — and loses the `lease_expires_at=None` write (`agents.py:768`). Same for `reconciliation.py` (~99, ~115) and `commands.py` (~769, ~779).
- [ ] Run-start metric: the inline path calls `observe_run_start(kind, run.enqueued_at, …)` (`agents.py` ~1072, `commands.py:742`). Remove both calls, `observe_run_start` and `run_start_latency_seconds` (`core/metrics.py` ~233) and their case in `test_metrics_runs.py` (~66) **before** touching the attribute, or every inline run raises `AttributeError`.
- [ ] Models: only after the writes and the read above are gone, remove the mapped attributes `enqueued_at`, `claimed_at`, `worker_id`, `lease_expires_at`, `attempt`, `heartbeat_at` from `AgentRun` and `CommandRun`, and from any response schema that exposes them. The columns stay in the database for A10.
- [ ] Fleet sampler and metrics: remove `fleet_queue_depth`, `run_queue_depth`, `run_queue_wait_seconds`, the lease and reaper counters and `observe_queue_wait` (`core/metrics.py` ~36–105, ~148, ~228) together with the sampler's use of `waiting_run_counts`. The sampler's per-team gauges and `ping_redis` stay.
- [ ] `capacity.py`: keep the run caps and their advisory-lock order; remove `waiting_run_counts` (D1 already removed queue depth).
- [ ] `runtime.py`: keep the in-memory registry, bus and lock; delete the `RedisEventBus` class (D1 already stopped choosing it).
- [ ] Startup reconciliation: keep the whole-table sweep, remove the worker scoping; the shutdown sweep "scoped to the runs this process owned" keeps working from the run registry, not from a `worker_id`. This is now the only recovery path: its test covers agent runs, command runs, Spec Kit phases and verification runs.
- [ ] Retention now always runs in the API. Behaviour unchanged.
- [ ] Settings: remove `run_queue_name`, `worker_id`, `worker_max_jobs`, `worker_max_tries`, `worker_job_timeout_seconds`, `run_lease_seconds`, `run_heartbeat_seconds`, `run_reaper_interval_seconds`, `run_unclaimed_grace_seconds`, `run_queue_max_depth`, and `metrics_port` if the inventory shows only the worker served it; add each to `REMOVED_SETTING_NAMES`.
- [ ] Tests: delete `test_redis_event_bus.py`; trim the both-mode files. `test_event_streaming.py` keeps every backfill-then-tail case: streams still read Postgres first and treat the bus as a hint.
- [ ] Redis pin: `redis<6` was for arq, but `opentelemetry-instrumentation-redis` may also constrain it. Try lifting it; if resolution or the rate-limit tests fail, leave it and say why.
- [ ] `AGENTS.md`: gotchas 16, 20–23, 26 (drop the worker, lease and `worker_id` sentences; keep the rules that still hold: persist before publish, Postgres is authoritative, no module-level state).

**Verify.** Invariants; `grep -rnE "lease_|_lease|\blease\b|RedisEventBus|worker_id|claimed_at|enqueued_at|publish_control" backend/app` returns nothing (a bare `lease` would match "release"); kill the backend mid-run and restart: the run is `error` / `interrupted_by_restart` and a new run can start; stop a running agent run from the UI.

**Exit.** One process owns its runs from start to finish; a restart is the only failure to recover from, and one sweep handles it.

## D4 — A single team never sees the hierarchy

**Context.** The data model stays Project → Team → Workspace, because team membership is how anyone but the owner gets into a workspace (`services/access.py`) and budgets are scoped to teams. For one team that hierarchy is ceremony before the first workspace. This step makes it automatic and invisible while there is only one. No schema change, and **nobody gains access they do not have today**.

Facts that shape it (verified):
- The **owner is the first registered user** (`services/users.py:36`), inside a `session.begin()` block (~26); nobody else is ever owner. `/setup` is only the LLM gate and can be skipped, so "finished setup" is not an event to hook.
- **Registration is open**: there is no invitation or closed-registration setting (`api/auth.py`, `core/config.py`). A later registrant can see nothing today.
- Reaching a team needs a **`ProjectMember` row as well as a `TeamMember` row** (`services/projects.py` ~40–50, ~66–73, ~216–222).
- A team viewer can read every team workspace's record, masked env keys and agent transcripts. Terminal, files and preview are owner-only for everyone.
- `ProjectService.create` and `create_team` both commit (~98, ~290), so they cannot be called inside the registration transaction.
- `POST /workspaces` without `team_id` creates a **personal** workspace, private to its owner, one per owner (`services/workspaces.py` ~219–226).

Decision that follows (2026-11-28, replacing an earlier draft that auto-joined new users as viewers): **no automatic membership.** With open registration, auto-joining would give anyone who can reach the sign-up page read access to every team workspace and transcript. A later registrant stays outside until the owner or a lead adds them, exactly as today; this step only makes adding them one obvious place.

- [ ] Defaults: in the registration transaction, when the new user is the first user (the advisory lock that makes them owner is already held), insert one default project, the owner's `ProjectMember` row, one default team and the owner's `TeamMember` row as lead — **rows inserted directly**, not through the committing service methods. For an installation that already has users and no project, create them on the owner's first `GET /projects`. Idempotent; never touches an installation that has a project.
- [ ] "The default team" needs no new column: it is the oldest team of the oldest project the caller can access. State that rule in one helper and use it everywhere.
- [ ] "Single team" is decided **per caller**, from the projects and teams that user can access. Exactly one of each → the UI skips the choosers. More → it shows them as today. None (a registrant nobody has added yet) → a plain page: "Ask the owner to add you to the team", plus their personal workspace if they have one.
- [ ] Adding people: in the single-team view, one Members page adds a user to the default team **and** its project in one action (both rows), with the role chosen there. The copy next to each role says what it grants (a viewer reads workspaces and agent transcripts; a member can also run agents).
- [ ] Workspaces: nothing changes about which route creates what. In the single-team view "New workspace" creates it in the default team and says the team will see it. Personal workspaces keep existing, stay private, and **stay listed** next to the team's.
- [ ] Frontend: with one accessible project and team, `/` and "Projects" lead to that team's workspaces plus the user's personal ones; the sidebar's Project and Team sections are hidden (`components/AppSidebar.tsx`; Track A step A9 edits the same file — rebase). Every existing route still resolves.
- [ ] Do not remove `teams`, `team_members`, `project_members`, `workspaces.team_id`, `RUN_CAP_PER_TEAM` or team-scoped budgets.
- [ ] Tests: first-user defaults (all four rows) and idempotence; the lazy path; **a second registrant has no membership and sees no team workspace**; adding a member creates both rows; the single-team shortcut, the multi-team chooser and the no-membership page; personal workspaces stay visible and private. Many test modules call `/auth/register` against the shared dev database: make sure the first-user branch cannot fire there (it only fires when the users table is empty) and that no test starts depending on a default team existing.

**Verify.** Invariants; a fresh database: register → a workspace, with no project or team screen; register a second user: they see no team workspace until the owner adds them from the Members page.

**Exit.** A single team gets from first login to a workspace without learning the hierarchy, adding a teammate is one action, nobody gains access they did not have, and an installation with several teams is unchanged.

---

## Columns handed to Track A's contract migration

Track A step A10 owns the one contract migration (`2026-11-28-enigma-lean-cut.md`). After D2 has been live for a release, A10 also drops `enqueued_at`, `claimed_at`, `worker_id`, `lease_expires_at`, `attempt` and `heartbeat_at` from `agent_runs` and `command_runs`. No index references them (`0033_run_leases` deliberately added none).

## Left alone on purpose

- **Redis itself**, for rate limiting. Replacing it with an in-process limiter would remove a service from the self-host stack, but it is a separate change with its own failure modes (limits reset on restart).
- **Flattening the hierarchy in the database.** Revisit only if D4 turns out not to be enough.
- **Kubernetes base manifests, tracing, retention, run caps, PgBouncer option, the stateless preview proxy.**
- **A time limit for inline runs.** There has never been one; the removed `agent_run_timeout_seconds` only bound worker runs. Worth adding later as its own change.

## What this changes in the other plans

Once D1 and D2 are merged, in Plans 11 and 12: ignore every task that says "queue mode", "a job that defers with `Retry`", "never await an agent run inside a job", "the reaper", "`worker_job_timeout_seconds`" or "redelivered job". What remains of each such task is the inline half: a `run_registry` task, conditional status writes, and the startup sweep covering the new state. Both plans carry a note pointing here.

## Exit criteria for the track

- `RUN_EXECUTION_MODE` no longer exists; asking for `queue` fails startup with a clear message.
- No worker, job, lease, reaper or cross-process event bus in the code, the compose file or the manifests.
- A restart mid-run leaves every kind of run in a terminal state and the workspace usable.
- The Kubernetes API is one replica that cannot be scaled by accident, and the docs say what a deploy does to a running run.
- A fresh installation reaches a first workspace without a project or team screen, and no user gains access they did not have.
