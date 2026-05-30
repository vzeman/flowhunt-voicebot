# Horizontal Scalability and Worker Deployment

The voicebot should run as a FlowHunt-native, workspace-scoped system. One
workspace can own many voicebots, and one voicebot can handle many concurrent
sessions.

## Worker Roles

Core roles:

- `media_ingress`: SIP/WebRTC/provider media ingress and egress
- `session_orchestrator`: call/session lifecycle, routing, cancellation
- `stt_worker`: speech-to-text provider calls
- `tts_worker`: text-to-speech provider calls and cache lookups
- `agent_worker`: communication agent turns
- `task_poller`: delegated subagent task polling
- `api`: admin/runtime API

These roles are modeled in `voicebot/scaling.py` so queue names, routing keys,
and limits are explicit and testable.

`WorkerRegistry` models runtime presence for these roles. Workers heartbeat with
`worker_id`, role, queue, optional workspace/voicebot affinity, capacity, and
status. Worker presence records require a worker id, queue, and positive
capacity. The registry requires a positive heartbeat TTL, can list active
workers by role/workspace, mark a worker as draining, remove a worker, expire
stale heartbeats, and summarize active worker capacity by role. Runtime can use
`JsonWorkerRegistry` for local restart recovery via
`VOICEBOT_WORKER_REGISTRY_STORE_PROVIDER=json` and
`VOICEBOT_WORKER_REGISTRY_STORE_PATH=/data/worker_registry.json`; production
should back the same contract with Redis or FlowHunt shared state.

A worker id is bound to its role and queue. Heartbeats may update status,
capacity, workspace affinity, and heartbeat time, but they cannot silently move
the same worker id to another role or queue.

## Routing

Every unit of work should carry:

- `workspace_id`
- `voicebot_id`
- `session_id`
- optional provider name

The routing partition is `workspace_id:voicebot_id:session_id`. Provider
rate-limits use `workspace_id:voicebot_id:provider`.

## Runtime Scaling API

`GET /scaling/topology` exposes the configured worker roles, queue names,
concurrency, and backpressure limits.

`POST /scaling/workload-plan` accepts a workspace, voicebot, expected concurrent
session count, optional session id, provider names, baseline sessions, call
growth rate, worker warm-up seconds, hard session caps, burst allowance, and
whether scale-to-zero is allowed. It returns the queue bindings, partition key,
provider keys, projected peak sessions, warm-capacity policy, and whether the
requested concurrency fits workspace/voicebot/provider/hard-cap limits. This is
a planning surface for FlowHunt deployment orchestration; it does not enqueue
work itself.

`GET /scaling/signals` exposes autoscaling inputs in JSON. Use
`GET /scaling/signals?format=prometheus` for Prometheus text format. Signals
include active sessions, per-workspace/voicebot session counts, queue pending
and claimed depth by worker role, dead-lettered queue items, provider failures,
latency metrics, calls per second, worker capacity, and warm-capacity deficits.

`POST /scaling/admission` evaluates whether a new session can be accepted before
allocating peer connections, SIP/media resources, or provider work. It compares
active sessions for the workspace/voicebot with `max_concurrent_sessions` and
`burst_sessions`. Accepted sessions return `decision=accept`; full hard capacity
with remaining burst returns `decision=queue_or_overflow`; full burst capacity
returns `decision=reject` and emits a `capacity_rejection` metric.

`POST /routing/admission` is the incoming-session preflight for routed SIP and
WebRTC sessions. It resolves the channel binding, verifies workspace access,
voicebot enabled state, provider/runtime configuration, capacity, and optional
session lease acquisition. It emits `session_admission_decided` for every
decision. Rejections include a deterministic reason and a transport-specific
fallback contract, such as SIP busy/unavailable or a WebRTC HTTP error before
SDP answer. The local direct Docker path remains permissive unless a caller uses
this routed admission API or passes routed metadata through a transport path.

`WorkerQueueEnvelope` defines the payload shape for future queue/stream
handoff. Every queued item carries an item id, idempotency key, work kind,
queue name, routing key with workspace/voicebot/session/provider fields,
payload, trace id, creation timestamp, retry attempt, and maximum attempts. The
routing partition key keeps all work for a session addressable after worker
restart, while the provider key supports provider-specific rate limits.

`WorkerQueueStore` is the local lifecycle contract for these envelopes. It can
enqueue pending items, deduplicate active submissions by idempotency key, claim
them by queue with an owner and TTL, renew claims, acknowledge completed work,
release failed work back to pending, expire abandoned claims, dead-letter work
after retry exhaustion, and produce grouped pending/claimed/dead-letter
snapshots. It is intentionally in-memory; the same lifecycle should move to
Redis streams, a database queue, or FlowHunt shared infrastructure for
production.
`JsonWorkerQueueStore` persists this local lifecycle for restart recovery during
development and single-node deployments.

`GET /scaling/backpressure`, `POST /scaling/backpressure/acquire`, and
`POST /scaling/backpressure/release` expose the local backpressure contract for
separated workers. Requests carry `workspace_id`, `voicebot_id`, and optional
`session_id` or `provider`. Provider requests use
`workspace_id:voicebot_id:provider` as the rate-limit key; otherwise the
workspace/voicebot/session partition key is used. The local max inflight value
is configured with `VOICEBOT_SCALING_BACKPRESSURE_MAX_INFLIGHT`.

Internal queue endpoints expose this lifecycle for early worker separation:

- `GET /scaling/queue`
- `POST /scaling/queue/enqueue`
- `POST /scaling/queue/claim`
- `POST /scaling/queue/renew`
- `POST /scaling/queue/ack`
- `POST /scaling/queue/release`
- `GET /scaling/queue/dead-letter`

`POST /scaling/workers/heartbeat` records process-local worker presence for a
worker id, role, queue, optional workspace/voicebot affinity, capacity, and
status. `GET /scaling/workers` lists active workers and can filter by role,
workspace, and voicebot. `GET /scaling/capacity` summarizes active capacity by
role and can also filter by workspace and voicebot. Global workers with no
workspace or voicebot affinity are included in narrower summaries because they
can serve any voicebot. `POST /scaling/workers/{worker_id}/drain` marks a worker
as draining, and `DELETE /scaling/workers/{worker_id}` removes a presence
record. This API is a runtime contract for orchestration; production should back
the same shape with Redis or FlowHunt shared state.

## Shared State

Production shared state:

- FlowHunt DB for sessions, events, transcripts, external task records, and
  workspace/voicebot config
- Redis or equivalent for active session leases, worker locks, media node
  presence, and short-lived counters
- workspace event stream for worker handoff

The JSON stores in this repository define the protocol; they are not the final
cloud storage implementation.

## Backpressure

Backpressure must exist at multiple levels:

- per workspace
- per voicebot
- per provider
- per worker role

`WorkspaceBackpressure` is the local contract for this accounting. It requires a
positive inflight limit and non-blank keys, so invalid capacity controls fail
before accepting or releasing work. The runtime API exposes acquire/release so
workers can reject or delay work before calling STT, TTS, agent, or subagent
providers.

STT and TTS workers also need provider-aware limits because external APIs can
rate-limit independently from workspace capacity.

## Warm Capacity

Warm capacity maps the min/max agent model to FlowHunt voicebots:

- `min_media_sessions`
- `min_stt_workers`
- `min_tts_workers`
- `min_agent_workers`
- `min_task_pollers`
- `max_concurrent_sessions`
- `burst_sessions`
- `scale_to_zero_allowed`

The local defaults keep one warm worker/capacity unit for every critical role.
Scale-to-zero can be used for demos, but production phone calls should keep
media, STT, TTS, agent, and task poller capacity warm enough to cover baseline
sessions plus expected growth while workers start.

Capacity planning uses:

```text
projected_peak_sessions = baseline_sessions + call_growth_per_minute * worker_warmup_seconds / 60
```

Round the result up and provision enough workers for the projected peak, not
only the current active sessions.

## Restart Behavior

On worker restart:

- media ingress reconnects or marks sessions as interrupted
- session orchestrator reloads active sessions from shared state
- task pollers resume pending tasks from durable task records
- agent workers claim pending events with durable leases
- completed late subagent results are stored as late results, not spoken into
  closed calls

Session ownership is represented by `/scaling/session-leases/*`. Lease records
include workspace, voicebot, session, owner, expiry, and optional call/transport
metadata. Local Docker can explicitly expire abandoned leases and enforce owner
loss with `/scaling/session-leases/enforce`. Enforcement stops live media that
cannot be safely recovered, emits `session_interrupted`, and emits
`session_recovered` for non-media work that can continue on another worker.

## Media Nodes

Media nodes should be disposable. They should announce presence through shared
state, accept routed sessions, and stop receiving new sessions during drain.
Session state must remain outside the media node so another worker can inspect
or clean up after failure.
