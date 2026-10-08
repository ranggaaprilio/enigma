# Epic 03 — Distributed Event Streaming & Pub/Sub

## Goal

Replace every in-process fan-out queue with a **shared event bus** so any API replica can
stream any run's events to a client, and a `stop` request from any replica reaches the worker
actually executing the run. This is the second half of the keystone: epic 02 moves *execution*
off the API; this moves *streaming and control signals* off process memory.

## User Value

A user's live agent/terminal/command stream works no matter which replica their WebSocket
lands on. Stop buttons actually stop the run. Notifications reach every open tab across
replicas.

## Current State

- Agent event fan-out is an in-process set of `asyncio.Queue`s:
  `agent_runtime.subscribe(run_id)` (`api/agents.py:310`), backed by
  `AgentRuntime.subscribers` (`agents.py:53`, published via `agents.py:89-91`).
- Custom commands have their own in-process `subscribers` (`commands.py:42-43`).
- Notifications fan out via `NotificationService.subscribers` (`notifications.py:18`,
  `defaultdict(set)` of queues).
- Stop is process-local: `request_stop` sets an `asyncio.Event` and calls `provider.pause()`
  (`agents.py:70-74`) — only effective inside the owning process.
- Terminal streams are a live PTY WebSocket bridged straight into the sandbox
  (`api/terminal.py`, `daytona_runtime.py` PTY) — inherently a single duplex connection (see
  note below).

## Scope

- A pub/sub `EventBus` abstraction with a Redis implementation (Redis Pub/Sub for fan-out;
  Redis Streams if replay/at-least-once is wanted).
- Rewrite agent, command, and notification streaming to publish/subscribe over the bus.
- A distributed **control channel** for `stop`/`pause`/`resume`.
- A durable **backfill-then-tail** pattern so a reconnecting client misses nothing.

## Design

- **Fan-out**: workers `publish(channel, event)`; API WS handlers `subscribe(channel)` and
  forward to the socket. Channels are keyed by entity: `run:{run_id}`, `cmd:{run_id}`,
  `user:{user_id}` (notifications), `workspace:{workspace_id}`.
- **No lost events on connect**: the WS handler first reads persisted events from Postgres
  (agent events are already stored — `event_payload` in `agents.py:97`), sends them, then
  subscribes to the live channel and de-dupes by event id. This is the correct fix for the
  "must land on the owning replica" problem *and* for reconnects (which epic
  `improvement-epics/07` also wants).
- **Control signals**: `request_stop` publishes to `control:run:{run_id}`; the worker running
  that job subscribes and, on receipt, sets its local stop event and calls `provider.pause()`.
  The public API of `request_stop` (`agents.py:70`) is unchanged; only the transport changes.
- **Redis Pub/Sub vs Streams**: start with Pub/Sub (simplest, and Postgres already provides
  durability for agent events via backfill). Consider Streams only for channels lacking a DB
  backing (e.g. raw command stdout) where replay matters.
- **Terminal**: PTY is a stateful duplex bridge to one sandbox and does **not** fan out; it
  only needs to reach the sandbox, which any replica can do via `DaytonaRuntime`. So terminals
  are already replica-agnostic — just confirm no hidden process-local session map is required
  and document it. (If per-session state is added later, route it through the bus too.)

## Backend Tasks

- Implement `EventBus` (Redis) behind the interface introduced in epic 01; keep the in-memory
  impl for tests/local single-process mode.
- Replace `AgentRuntime.publish/subscribe/unsubscribe` (`agents.py:76-91`) internals with bus
  calls; keep method signatures so `api/agents.py` barely changes.
- Backfill-then-tail in `stream_agent_run` (`api/agents.py:281-331`): load persisted events,
  send, then subscribe; de-dupe by event id; keep the existing ping keepalive
  (`api/agents.py:321`).
- Same treatment for command streaming (`commands.py`) and notification streaming
  (`notifications.py`).
- Route `stop` through the control channel (`agents.py:70`), so a stop accepted on replica A
  reaches the worker on host C.
- Ensure `provider.pause()` semantics still hold when the pause arrives via the bus.

## Frontend Tasks

- WebSocket reconnect with backoff for agent, command, and notification streams (helpers exist
  in `lib/api.ts`; this is the same work `improvement-epics/07` calls for).
- On reconnect, rely on backend backfill + client-side de-dupe by event id; drop any
  assumption that a stream starts "from now".
- Reconnecting/offline indicator so a replica rollover looks like a blip, not a failure.

## Database Tasks

- Ensure `agent_events` has an index on `(agent_run_id, created_at, id)` to make backfill
  cheap (see epic 05 for retention).
- If command stdout isn't persisted and replay is required, add a lightweight append store or
  use Redis Streams for that channel only.

## Security Requirements

- Authorize **before** subscribing: reuse the existing membership/ownership checks in the WS
  handlers (`api/agents.py:285-299`) before joining any channel.
- Namespace channels by tenant IDs so a subscription can't wildcard across workspaces/users.
- Don't publish secrets onto the bus; events are already sanitised — keep that invariant.

## Acceptance Criteria

- With two API replicas and a worker, a client connected to replica **B** receives the full,
  in-order event stream of a run executing under a worker triggered via replica **A**, with no
  gaps and no duplicates.
- Clicking stop on replica B halts a run whose worker was triggered via replica A.
- Killing the client's replica and reconnecting to another replica resumes the stream with no
  missing events (backfill + de-dupe verified).
- Notifications reach all of a user's open tabs regardless of replica.

## Implementation Notes (shipped)

Most of the transport landed with epic 02 (its "epic 03 slice"): `RedisEventBus` behind the
`EventBus` protocol (`services/runtime.py`), selected by `RUN_EXECUTION_MODE=queue`; the stop
control channel `agent_run_control:{id}` with a 5 s status re-check on the worker; and the agent
WebSocket's subscribe → `wait_ready` → catch-up. Topic names stayed as shipped
(`agent_run:{id}`, `command_run:{id}`, `notifications:{user_id}`) rather than the `run:`/`cmd:`/
`user:` names above. This epic closed the remaining gaps:

- **Invariant: Postgres is authoritative, the bus is an at-most-once hint.** Every stream
  subscribes, waits until the subscription is live, *then* reads persisted state, and re-reads on
  idle ticks and before closing. Publishers persist first and publish after the commit.
- **Agent events** — `AgentService.list_events` is now an indexed SQL query ordered by
  `(created_at, id)` with an optional inclusive `since`. The WebSocket keeps a high-water
  `created_at` and idle-tick catch-ups read only from it; once the run has ended (or a terminal
  frame arrives live) the catch-up re-reads everything, because a commit from another process (a
  stop accepted on another replica) can land late with an older timestamp. Migration
  `0032_agent_events_stream_index` adds `ix_agent_events_run_created_id` on
  `(agent_run_id, created_at, id)`.
- **Command output** — live `output` frames carry `offset`, their start in `run.output`, and
  contain exactly what was persisted. `tail_command_run` (`api/commands.py`) tracks how much it
  has sent: overlap is trimmed, a frame starting past it (an earlier one dropped) triggers a
  re-read, and a 5 s idle timeout re-reads too, so a lost status frame still ends the stream.
  Behaviour change: output past `AGENT_MAX_COMMAND_OUTPUT_CHARS` is no longer streamed live —
  live viewers now see exactly what a reconnect or a reload shows. No Redis Streams were needed.
- **Notifications** — on every reconnect after the first open, the bell re-fetches
  `GET /notifications` and merges by id (`NotificationBell` in `DashboardShell.tsx`), so anything
  created while the socket was down appears. The list endpoint is the durable record.
- **Frontend reconnect** was already in place (`useReconnectingWebSocket`: backoff, `/ready`
  gating during deploys, reconnecting banner). Agent and command streams clear their buffer on
  every (re)connect and rely on the backend replaying from Postgres.
- **Terminal** — confirmed replica-agnostic: the PTY WebSocket is a duplex bridge from whichever
  replica accepts it to the sandbox via `DaytonaRuntime`; terminal sessions are DB rows and no
  process-local session map exists. If per-session state is ever added, route it through the bus.
- **Security** — unchanged: each WebSocket authenticates the session cookie and checks
  workspace access (and run ownership, for commands) before subscribing; topics are exact
  `SUBSCRIBE`s on UUID-keyed channels (no pattern subscriptions), and access is re-checked on
  every Postgres re-read, so revoked access ends a command stream with 1008.
- **Tests** — `tests/test_event_streaming.py`: ordered/incremental backfill, the late-commit
  case, command offset trimming/gap fill/lost status frame, and acceptance tests over three
  `RedisEventBus` instances on one fakeredis server (output reaches replica B, survives a hop to
  replica C with no gaps or duplicates; one notification reaches tabs on two replicas). Stop
  across replicas is covered by
  `tests/test_redis_event_bus.py::test_stop_published_by_the_api_pauses_the_worker_provider`.
  Frontend: `NotificationBell.test.tsx`.

### Not covered

- **Stop for command runs, and pause/resume for any run** — no such endpoints exist; the control
  channel carries only `stop` for agent runs.
- **Live agent frames can arrive out of order** when the bus drops one and a later one arrives
  live: the dropped frame is delivered on the next catch-up (≤ 20 s), after it. Fixing that needs
  per-run sequence numbers (epic 04).
- **`agent_events.created_at` is `now()` (transaction start), not commit order.** An event from
  a long transaction can carry an older timestamp than ones already sent; if the bus also drops
  its frame, it arrives only with the final full re-read when the run ends (self-healing, but
  late). A monotonic per-run sequence column as the cursor would fix it (epic 04).
- **Revocation takes effect on the next re-read**: while frames flow, live frames are forwarded
  without re-checking access (≤ 20 s idle for agents, 5 s for commands). Same as before.
- **A command stream has no upper bound** while its run row stays `running`; it re-reads every
  5 s until the client leaves or reconciliation ends the run.
- **Agent stream handler is not unit-extracted** like `tail_command_run`; its acceptance rests on
  `catch_up` tests plus the bus tests.
