# Durable State

The prototype now has first durable primitives for restart recovery and scoped
queries. These are not the final FlowHunt database implementation, but they make
the storage contracts explicit and testable.

## Event Log

`JsonEventStore` persists every event as JSONL and reloads it on startup. It
preserves the next event id, so restart does not create duplicate event ids.
Reload diagnostics are available on the store as `load_diagnostics`, including
loaded events and skipped blank, malformed JSON, or invalid event rows. Corrupt
rows are skipped so restart recovery can continue, but operators can still see
that the local log needs attention.

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

## External Tasks

`JsonSubagentTaskStore` persists delegated subagent tasks, including provider
task ids, status, retry state, deadline, and terminal-event emission markers.
It exposes `load_diagnostics` with loaded task count plus skipped malformed JSON
and invalid task rows, so delegated task recovery can continue while local
storage corruption remains visible.

## Agent Task Leases

`JsonAgentTaskTracker` persists communication-agent task state:

- responded event ids and retention floor
- active worker claims with absolute lease expiration

On restart, unexpired claims are restored and expired claims are dropped. This is
still a local JSON implementation, but it makes the lease persistence contract
explicit before the tracker moves to Redis or FlowHunt database-backed leases.

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
