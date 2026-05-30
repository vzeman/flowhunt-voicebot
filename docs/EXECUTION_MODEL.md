# Voicebot Execution Model

This document defines the canonical execution model for the FlowHunt voicebot architecture tracked by issue #80.

The current runtime can still use existing `call_id`-based APIs, but new architecture work should use the identifiers and frame categories below so the system can evolve toward workspace-scoped, horizontally scalable voicebots.

## Core Scope

Every runtime item should be attributable to this scope:

```text
workspace_id -> voicebot_id -> session_id
```

Definitions:

- `workspace_id`: FlowHunt workspace and tenancy boundary.
- `voicebot_id`: configured voicebot inside a workspace.
- `session_id`: one customer conversation/call session.
- `call_id`: transport-specific call identifier. During the prototype, `call_id` often acts as `session_id`.

New runtime code should prefer `EventStore.append_scoped()`. It combines
`ExecutionScope`, `ExecutionIds`, and event-specific payload data so events
consistently carry workspace, voicebot, session, trace, turn, request, response,
and external task identifiers. Canonical execution scope and correlation IDs
take precedence over event-specific payload keys with the same names.

Use `ExecutionScope.same_session()` when deciding whether a response,
cancellation, or playback/control frame belongs to the same conversation. The
comparison requires matching workspace, voicebot, and session identifiers; it
does not rely on transport-specific `call_id` equality.

Pipeline-generated events use the same path through `EventLogProcessor`, which
extracts scope and correlation identifiers from frames before persisting events.
This keeps events emitted by processors aligned with events emitted directly by
API and task-lifecycle code.

## Correlation Identifiers

Use these identifiers consistently across events, frames, logs, and provider calls:

- `frame_id`: one pipeline frame.
- `event_id`: persisted event id.
- `turn_id`: one caller speech turn.
- `request_event_id`: event that started a tool/subagent/action.
- `response_to_event_id`: event answered by a response/action.
- `request_frame_id`: frame that started a downstream action.
- `response_to_frame_id`: frame answered by a downstream action.
- `external_task_id`: provider task id, such as a FlowHunt invoke task id.
- `trace_id`: cross-service trace correlation id.

## Frame Categories

The runtime pipeline should process typed frames/events in these categories:

| Category | Frame kinds |
| --- | --- |
| `audio` | `audio_input`, `audio_output` |
| `call_lifecycle` | `call_started`, `call_connected`, `call_ended` |
| `speech_lifecycle` | `speech_started`, `speech_finished` |
| `transcription` | `transcription_started`, `transcription_partial`, `transcription_finished`, `transcription_empty`, `user_transcript` |
| `agent` | `agent_request`, `agent_response_partial`, `agent_response`, `agent_response_dropped` |
| `tts` | `tts_started`, `tts_finished`, `tts_failed` |
| `playback` | `playback_started`, `playback_interrupted`, `playback_finished` |
| `call_control` | `dtmf`, `call_control_requested`, `call_control_completed` |
| `control` | `interrupt`, `cancel_agent`, `cancel_tts`, `pause_input`, `resume_input`, `flush_playback` |
| `metrics` | `metrics` |
| `system` | `system`, `error` |

## Pipeline Contract

The canonical stage contract is exposed by `voicebot.pipeline_contract` and the
HTTP endpoint:

`GET /pipeline/contract`

The contract is versioned with `pipeline_version`, and every live SIP/WebRTC
session snapshot plus lifecycle event includes that version. This makes session
logs auditable after pipeline changes and gives future queue workers a stable
compatibility boundary.

The current stage sequence is:

| Stage | Purpose | Queue boundary |
| --- | --- | --- |
| `transport_input` | Normalize SIP/Asterisk AudioSocket and WebRTC lifecycle/media input | `transport_owned` |
| `audio_normalization` | Resample, jitter-buffer, and echo-gate inbound audio | `in_process` |
| `turn_detection` | Detect caller turns and interrupt playback on barge-in | `in_process` |
| `stt` | Convert caller speech to transcript frames through replaceable STT providers | `queue_ready` |
| `communication_agent` | Produce customer-facing responses and request tools/subagents | `queue_ready` |
| `subagent_delegation` | Track provider-neutral colleague task lifecycle such as FlowHunt work | `durable_queue_required` |
| `tts` | Convert agent response chunks to audio through replaceable TTS providers | `queue_ready` |
| `playback_output` | Send interruptible audio to the active transport | `transport_owned` |
| `post_output_audit` | Persist transcripts, metrics, timelines, and retention artifacts | `queue_ready` |

`queue_ready` means the local Docker runtime still executes the stage in the
process, but the frame contract is explicit enough to move that work to a
durable queue later. `durable_queue_required` marks work that must be backed by
leases/retries before production Kubernetes failover.

Both `asterisk_audiosocket` and `webrtc` map to this same conceptual pipeline.
Transport-specific behavior belongs in adapters at the input/output edges, not
inside STT, agent, subagent, or TTS orchestration.

## Ordering Rules

Frames in these categories are session ordered:

- `call_lifecycle`
- `speech_lifecycle`
- `transcription`
- `agent`
- `tts`
- `playback`
- `call_control`
- `control`

The pipeline must preserve order for these frames within one `session_id`. Audio chunks can be streamed, but their lifecycle markers must still maintain session order.

Metrics and system frames may be observed out of band, but must carry enough correlation metadata to be attached back to the session timeline.

`FrameOrderingKey` is the canonical sort key for deterministic tests and
processor handoff boundaries:

```text
session_id, turn_id, timestamp, frame_id
```

`sort_frames_for_session()` applies this key. It is not a replacement for queue
ordering, but it gives runtime and regression tests one shared way to rebuild a
session-ordered view from buffered frames.

## Cancellation And Interruption

Cancellation/control frames are part of the normal execution model, not exceptional behavior.

Cancellation frames:

- `interrupt`
- `cancel_agent`
- `cancel_tts`
- `flush_playback`
- `call_ended`

Expected behavior:

- caller speech during bot playback emits an interruption/control path;
- playback stops for the affected session only;
- obsolete TTS and agent output should not be played after cancellation;
- cancellation must be correlated to the source event/frame where possible.

## Mapping From Current Prototype

Current persisted events map to frame categories through `voicebot.frame_events`.
`frame_event_mapping_issues()` validates that every persistable frame kind has a
declared event mapping and that mappings only point to declared frame/event
types. Raw audio frames and local cancellation/control frames are explicitly
marked as non-event frames.

Examples:

- `user_speech_started` maps from `speech_started`.
- `user_transcript` maps from `user_transcript`.
- `agent_response_requested` maps from `agent_request`.
- `agent_response_received` maps from `agent_response`.
- `flowhunt_flow_invoked`, `flowhunt_flow_updated`, and `flowhunt_flow_completed` should become external-task frames in the subagent/task framework.

## Implementation Notes

The compatibility layer lives in `voicebot.execution_model`.

It provides:

- `ExecutionScope`
- `ExecutionIds`
- frame category mapping
- session ordering helpers
- `FrameOrderingKey` and `sort_frames_for_session()`
- cancellation helpers
- extraction helpers for current frame metadata

Future issues should build on this model instead of adding provider-specific or transport-specific concepts directly to the pipeline.
