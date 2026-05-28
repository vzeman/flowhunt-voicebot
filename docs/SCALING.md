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
status. The registry can list active workers by role/workspace, mark a worker as
draining, remove a worker, and expire stale heartbeats. The first implementation
is in-memory; production should back it with Redis or FlowHunt shared state.

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
session count, optional session id, and provider names. It returns the queue
bindings, partition key, provider keys, and whether the requested concurrency
fits the current workspace/voicebot/provider limits. This is a planning surface
for FlowHunt deployment orchestration; it does not enqueue work itself.

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

STT and TTS workers also need provider-aware limits because external APIs can
rate-limit independently from workspace capacity.

## Restart Behavior

On worker restart:

- media ingress reconnects or marks sessions as interrupted
- session orchestrator reloads active sessions from shared state
- task pollers resume pending tasks from durable task records
- agent workers claim pending events with durable leases
- completed late subagent results are stored as late results, not spoken into
  closed calls

## Media Nodes

Media nodes should be disposable. They should announce presence through shared
state, accept routed sessions, and stop receiving new sessions during drain.
Session state must remain outside the media node so another worker can inspect
or clean up after failure.
