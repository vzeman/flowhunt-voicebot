# Durable State

The prototype now has first durable primitives for restart recovery and scoped
queries. These are not the final FlowHunt database implementation, but they make
the storage contracts explicit and testable.

## Event Log

`JsonEventStore` persists every event as JSONL and reloads it on startup. It
preserves the next event id, so restart does not create duplicate event ids.

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
