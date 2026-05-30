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
- required metadata for providers that need request-time inputs
- whether the provider returns clean result context or raw payloads

Descriptors are validated when a provider is registered. Invalid descriptors,
such as a blank label, blank required metadata keys, or cancellation support on
a non-polling provider, fail before the provider enters the catalog.

The coordinator validates required metadata before creating or submitting a task.
This catches malformed provider requests before a provider call is made and
keeps failure behavior consistent across future providers. FlowHunt flow and
project target IDs are integration configuration on the registered provider;
communication agents cannot supply or override `flow_id` or `project_id` in
tool-call metadata.

## Provider-Neutral Submission

The runtime exposes provider-neutral surfaces so the communication agent does
not need hardcoded language heuristics or FlowHunt-only behavior:

- `GET /subagent/providers`
- `POST /subagent/tasks`
- `POST /subagent/tasks/speculative`
- `POST /subagent/tasks/{task_id}/confirm-speculative`
- `POST /subagent/tasks/{task_id}/cancel-speculative`
- `POST /subagent/tasks/{task_id}/cancel`
- `delegate_to_subagent` agent tool

`POST /subagent/tasks` accepts workspace, session, request event, provider,
input text, optional voicebot id, dedupe key, and provider metadata. FlowHunt
providers ignore request metadata for target selection and use the configured
integration flow/project ID. When `schedule=true`, the task lifecycle runner
assigns the first poll/deadline.

Speculative delegation is available for partial-STT or intent-detector paths
that recognize stable external-work intent before final endpointing. A
speculative task starts with `metadata.speculative=true` and
`metadata.speculative_status=started`. Final STT must either confirm it with
`confirm-speculative` or cancel it with `cancel-speculative`. Completed
speculative results are held in task state and are not sent to the
communication agent until the task is confirmed, so speculative work can reduce
latency without speaking unconfirmed results to the caller.

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
3. poll the task by configured `flow_id + task_id`
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
These result responses are persistent across caller barge-in: playback may be
deferred or interrupted, but the completed colleague result is not discarded as
an ordinary stale answer before the caller has heard it.
Short colleague-result answers are kept as a single TTS phrase when possible,
and unrelated non-persistent responses are suppressed while the result is
actively queued so the caller hears the colleague result cleanly.
If a caller asks for historical information that was not answered by a recent
window result, such as the last downtime after a "no incidents in 90 days"
answer, the communication agent should re-delegate with an explicit archive or
history request instead of treating the limited result as complete.
The communication agent also runs a model-based recovery check when a draft
spoken answer promises checking or colleague work but contains no tool call. If
that check says the request still needs external work, the agent creates the
FlowHunt/subagent tool call for the current event before marking it answered.
This avoids language-specific keyword routing while preventing "I will check"
responses that do not actually start delegated work.

Late STT results are delivered to the communication agent as
`reason=stale_transcript` rather than being filtered by language-specific
keywords in the media runtime. This keeps multilingual call-control commands
and follow-up requests available to the model/tool-calling layer while still
making it clear that newer caller audio had already started.
The API also avoids language-specific vague-request filters for colleague
delegation. The communication agent is responsible for producing a clear
tool-call payload or asking the caller for clarification.

Remaining production follow-up:

- move subagent task leases to a shared multi-worker lease store
- enforce provider rate limits from workspace/voicebot provider config
- add more provider adapters beyond FlowHunt and internal workers
