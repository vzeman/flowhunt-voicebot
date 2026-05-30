# Subagent and External Task Framework

The communication voice agent should stay focused on the caller. Complex work is
delegated to subagents through a generic task lifecycle. FlowHunt is one
provider behind this contract, not a hardcoded behavior in the voice pipeline.

## Task Request

`SubagentTaskRequest` requires:

- `workspace_id`
- `session_id`
- `request_event_id`
- provider kind
- input text
- optional `voicebot_id`
- optional metadata

`workspace_id` is required so delegated work cannot cross FlowHunt workspace
boundaries.

## Lifecycle

Task statuses:

- `requested`
- `accepted`
- `running`
- `completed`
- `failed`
- `timed_out`
- `cancelled`

The first in-memory store deduplicates tasks by
`workspace_id + session_id + request_event_id`. This prevents repeated caller
turn processing from scheduling the same colleague task multiple times.
Stored task identity is immutable across updates: providers may change status,
progress, external ids, result, and error details, but cannot move a task to
another workspace, session, voicebot, provider, or request event.

## Clean Result Context

Providers can keep raw payloads, but the communication agent should receive only
clean task context:

- task id
- status
- provider
- summary
- content
- structured context

Raw provider payloads are stored for debugging and audit, but they are not meant
to be spoken directly to the customer.

## Provider Boundary

The generic provider interface has:

- `submit(request)`
- `poll(task)`
- `cancel(task)`

Providers also expose `SubagentProviderDescriptor` metadata through
`SubagentCoordinator.provider_catalog()`:

- provider kind and label
- workspace scoping
- async polling support
- cancel support
- required metadata such as `flow_id` or `project_id`
- whether the provider returns clean result context or raw payloads

Descriptors are validated when a provider is registered. Invalid descriptors,
such as a blank label, blank required metadata keys, or cancellation support on
a non-polling provider, fail before the provider enters the catalog.

The coordinator validates required metadata before creating or submitting a task.
This catches malformed FlowHunt flow/project requests before a provider call is
made and keeps failure behavior consistent across future providers.

## Provider-Neutral Submission

The runtime exposes provider-neutral surfaces so the communication agent does
not need hardcoded language heuristics or FlowHunt-only behavior:

- `GET /subagent/providers`
- `POST /subagent/tasks`
- `POST /subagent/tasks/{task_id}/cancel`
- `delegate_to_subagent` agent tool

`POST /subagent/tasks` accepts workspace, session, request event, provider,
input text, optional voicebot id, dedupe key, and provider metadata. When
`schedule=true`, the task lifecycle runner assigns the first poll/deadline.

The `delegate_to_subagent` tool derives workspace/session scope from the active
call route when available and falls back to local FlowHunt workspace settings
for Docker testing. It submits through `SubagentCoordinator`, so every provider
uses the same validation, dedupe, lifecycle, terminal-event, and late-result
behavior.

The communication-agent worker starts a short delayed acknowledgement while its
model decides whether a caller request needs colleague work. If that
acknowledgement has already been spoken, the worker sets `suppress_progress` on
the colleague tool call so the tool schedules work silently instead of repeating
another waiting phrase. Other agents can omit `suppress_progress` and receive
the default spoken progress update from the tool.

When default progress speech is enabled, the tool schedules that speech in
parallel with task creation. The caller can hear that work is starting while the
subagent task is already being submitted or polled; the task lifecycle is not
blocked by TTS generation or playback.

When an agent model returns both a `say` call and a colleague/subagent work call
in the same turn, the tool executor treats them as separate intents and
dispatches them concurrently. This keeps customer-facing speech responsive
without delaying task submission.

The first adapter is `FlowHuntSubagentProvider`, with provider kinds:

- `flowhunt_flow`
- `flowhunt_project`

For FlowHunt flow execution, the adapter uses the invoke task protocol:

1. submit with flow invoke
2. store the returned task id
3. poll the task by `flow_id + task_id`
4. complete only when the provider task returns a final result/status

## Runtime Integration

The current implementation is intentionally independent from HTTP routes and
live calls. `SubagentCoordinator` can now emit workspace-scoped lifecycle events
when it is constructed with an `EventStore`:

- `subagent_task_requested`
- `subagent_task_deduplicated`
- `subagent_task_updated`
- `subagent_task_cancelled`

Event payloads use `SubagentTask.event_context()`, which exposes clean task
status/result context and excludes raw provider payloads.

Completed task results are re-entered as `agent_response_requested` events when
the call is still active. The communication agent then normalizes greetings,
provider status text, markdown, links, and raw payload details before phrasing a
short spoken answer for the caller. The full colleague content remains in task
and transcript context, but the first spoken result is intentionally concise so
the caller can ask for details instead of waiting through a long readout.

Remaining production follow-up:

- move subagent task leases to a shared multi-worker lease store
- enforce provider rate limits from workspace/voicebot provider config
- add more provider adapters beyond FlowHunt and internal workers
