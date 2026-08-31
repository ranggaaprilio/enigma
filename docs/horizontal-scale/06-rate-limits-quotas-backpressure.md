# Epic 06 — Fair-Use: Rate Limits, Quotas & Backpressure

## Goal

Protect a shared multi-team instance from any one user, team, or workspace exhausting compute,
LLM spend, or connections. Add distributed rate limits, concurrency quotas, and backpressure
so load stays fair and bounded across replicas.

## User Value

One team's runaway automation can't degrade everyone else. Operators get predictable cost and
capacity per tenant. Users get clear "slow down / at capacity" signals instead of timeouts.

## Current State

- Rate limiting exists for **login only**: `RedisLoginRateLimiter` (`rate_limit.py:18`), keyed
  by email+IP in Redis. AI/agent/command endpoints are **unbounded** (also flagged in
  `improvement-epics/07`).
- The only concurrency control is one active run per workspace (`agents.py:158-162`) — nothing
  caps total concurrent runs per team or platform-wide.
- Budgets exist per workspace (`BudgetService.assert_within_budget`, `agents.py:165`) and are a
  good cost primitive to build tenant quotas on, but they don't throttle request rate or global
  concurrency.

## Scope

- Generalise the Redis rate limiter into a reusable, distributed limiter.
- Apply rate limits to expensive endpoints, keyed per user / team / workspace.
- Concurrency quotas: max concurrent runs per team and platform-wide (feeds the queue in
  epic 02).
- Backpressure: shed or queue load gracefully when limits are hit.

## Backend Tasks

- **Generalise the limiter**: extract the token-bucket/fixed-window logic from
  `RedisLoginRateLimiter` (`rate_limit.py`) into a `RateLimiter` usable with arbitrary keys and
  policies. Keep it Redis-backed so limits are shared across replicas.
- **Apply to expensive endpoints**: run creation (`api/agents.py`), command runs
  (`api/commands.py`), Spec-Kit phase triggers (`api/specs.py`), file uploads, sandbox
  provisioning. Policies keyed by user, team, and workspace; return `429` with `Retry-After`.
- **Concurrency quotas**: before enqueuing (epic 02), check active+queued run counts against a
  per-team and global cap held in Redis; reject or queue with a visible "queued" state.
- **Backpressure from the queue**: expose queue depth; when depth exceeds a threshold, apply
  stricter admission (this is also the autoscaling signal in epic 08).
- **Tie into budgets**: when a workspace/team is over its cost budget
  (`BudgetService`, `agents.py:165`), the limiter denies new runs consistently across replicas.
- WebSocket connection caps per user (prevent connection-exhaustion via many open streams).

## Frontend Tasks

- Handle `429` gracefully: surface "rate limited, retry in Ns" and auto-retry with backoff
  where safe.
- Show "queued (position/estimate)" when a run is admitted but waiting behind the concurrency
  cap.
- Disable the run/command buttons when the tenant is at quota, with an explanatory tooltip.

## Database Tasks

- Optional: persist per-team quota configuration (limits, concurrency caps) so admins can tune
  them; otherwise config-driven defaults.
- Index any quota-usage lookups that aren't served from Redis.

## Security Requirements

- Limits are enforced server-side and keyed off authenticated identity, never client-supplied
  values.
- Rate-limit state is tenant-namespaced; one tenant cannot read or reset another's counters.
- Denials fail closed (deny on limiter/Redis error for spend-bearing actions) with a clear
  message.

## Acceptance Criteria

- A single user hammering run creation is throttled with `429`+`Retry-After` and cannot starve
  other tenants.
- Per-team and global concurrent-run caps are enforced across multiple replicas (verified with
  two API instances).
- Exceeding a workspace/team budget denies new runs uniformly, regardless of replica.
- Open-WebSocket count per user is bounded.
- All limits are configurable and observable (current usage visible in metrics — epic 09).
