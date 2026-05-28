# Observability, Tracing, and Evaluation

The first observability layer provides reusable data models for debugging a
single call and regression-testing voicebot behavior without a live call.

## Trace Context

`TraceContext` carries the fields needed in logs, events, and provider calls:

- `trace_id`
- `workspace_id`
- `voicebot_id`
- `session_id`
- `call_id`
- `turn_id`
- `event_id`

`structured_log_record()` turns this context into JSON-ready log records.

## Timeline

`build_timeline()` converts call events into a sorted timeline with categories:

- call
- caller audio
- STT
- agent
- TTS
- playback
- task
- control
- transport
- telemetry
- system

This is the backend shape needed for a call timeline viewer.

The timeline also includes `audio_observability_summary()` output for quick
debugging of realtime behavior:

- speech turns started and finished
- STT no-text events and transcript counts
- playback starts, finishes, and interruptions
- possible barge-ins
- open speech/playback counters

The timeline response also embeds provider rollups so the same payload shows
provider latency sample counts, average latency, and failure counts.

`timeline_health_summary()` adds deterministic health flags to the same
timeline payload. It reports warnings for open speech turns, open playback, and
provider failures so operators can scan one response before inspecting the full
event list.

The timeline also reports `duration_seconds` when at least two event timestamps
can be parsed. This is the elapsed time between the first and last event in the
selected debug window.

## Provider Summary

`provider_observability_summary()` aggregates provider latency samples and
failure counts from events. The timeline response and existing metrics summary
include this provider rollup.

The runtime also emits `colleague_result_to_agent_request_seconds` when a
terminal subagent result is handed back to the communication agent. Use this
metric to separate FlowHunt/provider execution time from internal handoff
latency before the caller hears the final answer.

## Conversation Evaluation

`evaluate_conversation()` runs deterministic checks against an event sequence:

- required event types
- duplicate consecutive agent responses
- final agent response presence

This is the first regression-test harness for problems we observed during manual
testing, especially repeated responses and missing final answers after delegated
work.

## Runtime API

`GET /observability/timeline` returns a categorized event timeline. It accepts
`after`, `call_id`, `workspace_id`, `voicebot_id`, `session_id`, and `limit`
filters.

`POST /observability/evaluate` runs deterministic conversation checks against
the selected event window. The request can provide required event types,
duplicate response tolerance, and whether a final agent response is required.

## Next Step

Add runtime structured logging and audio fixture tests for VAD, STT quality,
barge-in, and playback interruption.
