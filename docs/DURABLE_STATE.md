# Durable State

The prototype now has first durable primitives for restart recovery and scoped
queries. These are not the final FlowHunt database implementation, but they make
the storage contracts explicit and testable.

`GET /health/readiness` exposes a `durable_storage` check for the configured
runtime stores. It reports store kind, path, load diagnostics, compact snapshot
counts, warning counts from skipped/requeued recovery rows, and path
writability. This makes restart-recovery problems visible through the service
API before the JSON stores move to FlowHunt database or Redis-backed storage.

`GET /storage/contracts` exposes the production shared-state contract catalog.
It lists every runtime state family, the local provider used by Docker/tests,
the future production backend class, required workspace/session scope fields,
idempotency keys, and consistency expectation. The same catalog is also checked
from `GET /health/readiness` under `storage_contracts`, so missing scope or
idempotency definitions fail readiness diagnostics before Kubernetes work starts.

`GET /storage/drivers` exposes the storage driver registry and the currently
selected driver for each family. It is the operational view of the same
abstraction layer: contracts describe what each family must do, while drivers
describe which implementation is selected in this environment.

The current contract families are:

| Contract | Local provider | Production backend direction | Idempotency |
| --- | --- | --- | --- |
| `events` | JSONL or memory | FlowHunt DB or append-only event log | `event_id` |
| `transcripts` | JSONL files | FlowHunt DB plus object storage for large artifacts | `event_id` |
| `voicebot_sessions` | JSON or memory | FlowHunt DB | `session_id` |
| `session_leases` | JSON or memory | Redis or equivalent lease-capable KV | `workspace_id + voicebot_id + session_id` |
| `agent_tasks` | JSON or memory | Redis and/or FlowHunt DB | `event_id` |
| `worker_queue` | JSON or memory | Redis Streams, NATS JetStream, RabbitMQ, or FlowHunt queue | `idempotency_key` or `item_id` |
| `worker_registry` | JSON or memory | Redis and/or FlowHunt DB | `worker_id` |
| `call_states` | JSON or memory | Redis and/or FlowHunt DB | `call_id` |
| `provider_config` | JSON or memory | FlowHunt DB plus FlowHunt/Kubernetes secret references | `workspace_id + voicebot_id + config_version` |
| `sip_trunks` | JSON | FlowHunt DB plus secret storage | `trunk_id` |
| `subagent_tasks` | JSON or memory | FlowHunt DB, Redis, and/or FlowHunt queue | `workspace_id + dedupe_key` |
| `audio_artifacts` | filesystem | object storage plus DB metadata index | `artifact_id` or `content_hash` |

Every production implementation must preserve the same public semantics as the
local provider: workspace-scoped reads, idempotent retries where expected,
diagnostics for skipped/recovered rows, and no raw secrets in API responses.

## Driver Configuration

Storage is selected per family by environment variables. Local Docker defaults
use JSON, JSONL, memory, and filesystem drivers. Managed drivers are registered
in the driver catalog so configuration and readiness can describe the production
target, but only local drivers are implemented in this slice.

| Family | Provider env var | Default | Path/config env var | Current drivers | Managed target |
| --- | --- | --- | --- | --- | --- |
| `events` | `VOICEBOT_EVENT_STORE_PROVIDER` | `json` | `VOICEBOT_EVENT_STORE_PATH` | `json`, `jsonl`, `memory` | `flowhunt_db`, `append_only_event_log` |
| `transcripts` | `VOICEBOT_TRANSCRIPT_STORE_PROVIDER` | `jsonl` | `VOICEBOT_TRANSCRIPT_DIR` | `jsonl` | `flowhunt_db`, `object_storage` |
| `voicebot_sessions` | `VOICEBOT_SESSION_STORE_PROVIDER` | `json` | `VOICEBOT_SESSION_STORE_PATH` | `json`, `memory` | `flowhunt_db` |
| `session_leases` | `VOICEBOT_SESSION_LEASE_STORE_PROVIDER` | `json` | `VOICEBOT_SESSION_LEASE_STORE_PATH` | `json`, `memory` | `redis` |
| `agent_tasks` | `VOICEBOT_AGENT_TASK_STORE_PROVIDER` | `json` | `VOICEBOT_AGENT_TASK_STORE_PATH` | `json`, `memory` | `redis`, `flowhunt_db` |
| `worker_queue` | `VOICEBOT_WORKER_QUEUE_STORE_PROVIDER` | `json` | `VOICEBOT_WORKER_QUEUE_STORE_PATH` | `json`, `memory` | `redis_streams`, `nats_jetstream`, `rabbitmq`, `flowhunt_queue` |
| `worker_registry` | `VOICEBOT_WORKER_REGISTRY_STORE_PROVIDER` | `json` | `VOICEBOT_WORKER_REGISTRY_STORE_PATH` | `json`, `memory` | `redis`, `flowhunt_db` |
| `call_states` | `VOICEBOT_CALL_STATE_STORE_PROVIDER` | `json` | `VOICEBOT_CALL_STATE_STORE_PATH` | `json`, `memory` | `redis`, `flowhunt_db` |
| `provider_config` | `VOICEBOT_PROVIDER_CONFIG_STORE_PROVIDER` | `json` | `VOICEBOT_PROVIDER_CONFIG_STORE_PATH` | `json`, `memory` | `flowhunt_db`, secret references |
| `sip_trunks` | `VOICEBOT_SIP_TRUNK_STORE_PROVIDER` | `json` | `VOICEBOT_SIP_TRUNK_REGISTRY_PATH`, `VOICEBOT_SIP_TRUNK_PJSIP_INCLUDE_PATH` | `json` | `flowhunt_db`, secret references |
| `subagent_tasks` | `VOICEBOT_SUBAGENT_TASK_STORE_PROVIDER` | `json` | `VOICEBOT_SUBAGENT_TASK_STORE_PATH` | `json`, `memory` | `flowhunt_db`, `redis`, `flowhunt_queue` |
| `audio_artifacts` | `VOICEBOT_AUDIO_ARTIFACT_STORE_PROVIDER` | `filesystem` | `VOICEBOT_TTS_CACHE_DIR`, `VOICEBOT_DEBUG_AUDIO_DIR` | `filesystem` | `object_storage`, CDN/cache |

Compatibility note: existing `json` and `jsonl` aliases are preserved for local
event storage, and object-style JSON stores continue to accept `jsonl` as an
alias for `json` so older local configuration does not break.

Readiness includes the selected driver metadata for constructed runtime stores
under `checks.durable_storage.stores[*].driver`. The `/storage/drivers` endpoint
also reports selected-but-not-yet-constructed families such as `provider_config`
and `audio_artifacts`.

## Driver Health And Errors

The storage abstraction layer defines shared health and error primitives so
local and managed drivers report failures consistently:

- `StoreHealth` describes whether a store is reachable, any warning counts, the
  selected driver metadata, load diagnostics, path writability, and compact
  runtime snapshots.
- `StorageError` carries a stable `code`, optional `family`, optional `driver`,
  a message, and structured details.
- Specialized error codes are `unavailable`, `conflict`, `not_found`,
  `validation_error`, `timeout`, and `corruption_warning`.

Managed drivers should raise these storage errors at their API boundary instead
of leaking backend-specific exceptions. API handlers and workers can then map
errors consistently to retries, dead-lettering, readiness failures, or user-safe
diagnostics.

Reusable contract tests live in `tests/storage_contract_cases.py`. New drivers
should run the same lifecycle tests as the local memory/JSON/JSONL drivers
before they are enabled in production configuration.

## Audio Artifacts

`FilesystemArtifactStore` is the first concrete audio artifact driver. It stores
binary artifacts under a configured root and writes sidecar metadata files with
the `.metadata.json` suffix. The TTS cache and speech-only call recordings write
through this artifact interface, so generated audio and recording playback files
follow the same `put/get/delete/list` contract that an object-storage driver
must implement later.

The current local TTS cache still uses:

- `VOICEBOT_AUDIO_ARTIFACT_STORE_PROVIDER=filesystem`
- `VOICEBOT_TTS_CACHE_DIR=/data/tts-cache`

Speech-only call recording is controlled by:

- `VOICEBOT_CALL_RECORDING_ENABLED=true`
- `VOICEBOT_CALL_RECORDING_SILENCE_THRESHOLD=0.003`

The current filesystem driver stores call recordings in the same local audio
artifact root. Recording metadata contains the compact playback duration and the
original call offsets for each captured caller or voicebot speech segment.

The managed production target remains object storage plus a DB metadata index.
Production object-storage drivers must keep artifact metadata workspace-scoped
and must not make recordings, debug audio, or generated speech publicly
readable unless a separate authenticated access layer issues a signed URL.

Retention and deletion policy is exposed separately by
`GET /workspaces/{workspace_id}/security/retention`. The policy defines
workspace-scoped deletion hooks for events, transcripts, recordings/debug
audio, cached TTS audio, and delegated task state. Local JSON/filesystem stores
only expose the contract; FlowHunt production storage must implement these hooks
against database rows, object storage metadata, and cache indexes.

## Event Log

`JsonEventStore` persists every event as JSONL and reloads it on startup. It
preserves the next event id, so restart does not create duplicate event ids.
Reload diagnostics are available on the store as `load_diagnostics`, including
loaded events and skipped blank, malformed JSON, invalid event rows, or
duplicate event ids. Corrupt rows are skipped so restart recovery can continue,
but operators can still see that the local log needs attention. Invalid event
rows include non-positive event ids and blank call id, event type, or timestamp
fields. When duplicate event ids are found during reload, the first row wins and
later duplicates are skipped.

The runtime selects the event store with:

- `VOICEBOT_EVENT_STORE_PROVIDER=json|memory`
- `VOICEBOT_EVENT_STORE_PATH=/data/events/events.jsonl`

Docker defaults to `json`, so local service restarts keep the event cursor and
subagent result history. Tests and embedded callers can still use the in-memory
store directly when durability is not required.

The event store can query by:

- `call_id`
- `workspace_id`
- `voicebot_id`
- `session_id`
- cursor `after`
- `limit`

Workspace and voicebot fields are read from event data. New runtime code should
include `workspace_id`, `voicebot_id`, and `session_id` in lifecycle events
whenever the transport route is known.

## Transcripts

`TranscriptStore` already persists per-call transcript/event JSONL files and
reports corruption statistics. It remains the call transcript surface.

## Provider Configuration

`JsonProviderConfigStore` persists workspace/voicebot provider selections for
STT, TTS, and the communication agent. It stores provider names, model choices,
fallback providers, provider-specific config objects, and secret references. It
does not store raw provider API keys.

The runtime selects the provider configuration store with:

- `VOICEBOT_PROVIDER_CONFIG_STORE_PROVIDER=json|memory`
- `VOICEBOT_PROVIDER_CONFIG_STORE_PATH=/data/provider_config.json`

Docker defaults to `json`, so provider selections changed through the
workspace/voicebot provider API survive local service restarts. Production
should replace this with FlowHunt database rows and FlowHunt secret references,
with versioned activation so active calls keep the config version they started
with.

## Sessions

`JsonVoicebotSessionStore` persists normalized voicebot session records,
including workspace, voicebot, channel, external session, status, timestamps,
and metadata fields. It exposes `load_diagnostics` with loaded session count,
malformed JSON count, skipped invalid rows, and skipped duplicate session ids.
Duplicate session ids use first-row-wins behavior during reload so corrupted
local files cannot silently move a session across workspace or voicebot scope.

The runtime selects the session store with:

- `VOICEBOT_SESSION_STORE_PROVIDER=json|memory`
- `VOICEBOT_SESSION_STORE_PATH=/data/voicebot_sessions.json`

Routed WebRTC sessions are persisted when their metadata includes
`workspace_id` and `voicebot_id`. On session close, the same durable record is
marked ended. Unrouted local test sessions are intentionally skipped because
they cannot be permission-scoped in FlowHunt product APIs.

## Session Leases

`JsonSessionLeaseStore` persists active session ownership leases:

- workspace, voicebot, and session id
- optional call id, transport kind, and routing metadata
- lease owner
- absolute expiration timestamp
- load diagnostics for malformed JSON, invalid rows, duplicate lease keys, and
  expired leases skipped during reload

The runtime selects the session lease store with:

- `VOICEBOT_SESSION_LEASE_STORE_PROVIDER=json|memory`
- `VOICEBOT_SESSION_LEASE_STORE_PATH=/data/session_leases.json`

Internal worker/session orchestration APIs use:

- `GET /scaling/session-leases`
- `POST /scaling/session-leases/acquire`
- `POST /scaling/session-leases/renew`
- `POST /scaling/session-leases/release`
- `POST /scaling/session-leases/expire`
- `POST /scaling/session-leases/enforce`

These leases are the local coordination contract for active sessions. In
production, the same semantics should move to Redis or another shared
lease-capable store.

Acquire and renew requests may include `call_id`, `transport`, and metadata
such as pod/node identifiers. The runtime emits `session_lease_acquired`,
`session_lease_renewed`, `session_lease_released`, and
`session_lease_expired` events for operational timelines.

`/scaling/session-leases/enforce` compares active session snapshots with the
lease store for the supplied owner. If a live media session is missing its
lease, or the lease belongs to another owner, the runtime emits
`session_lease_lost`. It can then stop the active media session and emit
`session_interrupted` while also emitting `session_recovered` for non-media
work that can continue, such as subagent polling, transcript storage, summaries,
and late task result handling.

## External Tasks

`JsonSubagentTaskStore` persists delegated subagent tasks, including provider
task ids, status, retry state, deadline, and terminal-event emission markers.
It exposes `load_diagnostics` with loaded task count plus skipped malformed JSON
invalid task rows, duplicate task ids, and duplicate workspace/dedupe keys, so
delegated task recovery can continue while local storage corruption remains
visible. Duplicate rows use first-row-wins behavior to keep restart recovery
deterministic and avoid silently moving dedupe indexes to later corrupt rows.

## Agent Task Leases

`JsonAgentTaskTracker` persists communication-agent task state:

- responded event ids and retention floor
- active worker claims with absolute lease expiration

On restart, unexpired claims are restored and expired claims are dropped. This is
still a local JSON implementation, but it makes the lease persistence contract
explicit before the tracker moves to Redis or FlowHunt database-backed leases.

The runtime selects the agent task tracker with:

- `VOICEBOT_AGENT_TASK_STORE_PROVIDER=json|memory`
- `VOICEBOT_AGENT_TASK_STORE_PATH=/data/agent_tasks.json`

Docker defaults to `json`, so communication-agent task responses and active
claim leases survive local service restarts within their configured TTL.

## Worker Queue

`JsonWorkerRegistry` persists local worker presence records:

- worker id, role, queue, workspace/voicebot affinity, capacity, and status
- last heartbeat timestamp
- load diagnostics for malformed JSON, invalid rows, duplicate worker ids, and
  expired workers skipped during reload

The runtime selects the worker registry store with:

- `VOICEBOT_WORKER_REGISTRY_STORE_PROVIDER=json|memory`
- `VOICEBOT_WORKER_REGISTRY_STORE_PATH=/data/worker_registry.json`
- `VOICEBOT_WORKER_REGISTRY_HEARTBEAT_TTL_SECONDS=30`

Docker defaults to `json`, so local worker presence and drain state can recover
after service restart within the heartbeat TTL.

`JsonWorkerQueueStore` persists the local worker queue lifecycle contract:

- pending worker queue envelopes by queue name
- active claims with owner and absolute expiration
- idempotency keys for duplicate active submission detection
- retry counters, maximum attempts, last error, and dead-lettered terminal rows
- expired claims requeued to pending on reload or dead-lettered when retry
  attempts are exhausted
- load diagnostics for malformed JSON, invalid rows, duplicate item ids, and
  recovered dead-lettered rows

The runtime selects the worker queue store with:

- `VOICEBOT_WORKER_QUEUE_STORE_PROVIDER=json|memory`
- `VOICEBOT_WORKER_QUEUE_STORE_PATH=/data/worker_queue.json`

Docker defaults to `json`, so internal `/scaling/queue/*` work handoff survives
local service restarts. Acknowledged items are removed from the local queue;
pending, claimed, and dead-lettered items remain visible for diagnostics.
local service restarts until production replaces it with Redis streams or
FlowHunt shared queue infrastructure.

## Production Direction

The JSON stores are implementation scaffolding. In FlowHunt production:

- durable entities should move to FlowHunt database tables keyed by workspace
- short-lived active session state and locks should use Redis or another
  lease-capable shared store
- worker events should move through a queue or stream
- event/task queries must always be workspace-scoped

The important contract is now explicit: sessions, events, transcripts, and
external task records all carry enough workspace/session metadata to be moved to
shared storage without changing the voice pipeline shape.
