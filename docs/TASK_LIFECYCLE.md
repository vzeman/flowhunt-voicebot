# Async Task Lifecycle and Polling

Subagent work must survive longer than one model turn and often longer than one
process. The lifecycle layer hardens delegated tasks before they are wired into
the active voice runtime.

## Runtime Integration

The voicebot service now creates a subagent coordinator at startup, registers
FlowHunt providers from runtime configuration, and runs a lifespan-managed task
poller. Agent tool calls can submit FlowHunt flow work through the generic
subagent task store instead of relying on per-request background watchers.

Completed, failed, timed-out, cancelled, and late-completed tasks produce
workspace-scoped subagent events. If the call is still active, completed and
failed tasks also enqueue an `agent_response_requested` event so the
communication agent can present the colleague result naturally to the caller.

`GET /subagent/tasks` exposes stored delegated work for debugging and operations,
with optional `workspace_id` and `session_id` filters.

## Durable Task References

`JsonSubagentTaskStore` persists:

- internal task id
- workspace id
- session id
- request event id
- provider kind
- external provider task id
- dedupe key
- status
- attempts and next poll time
- deadline
- terminal event emission marker
- provider references and raw result payloads

This is a first durable implementation. Production storage should move to the
FlowHunt database, but the protocol is now explicit and restart-safe.

## Polling Policy

`SubagentTaskLifecycleRunner` applies:

- initial poll interval
- max poll interval
- exponential backoff
- max attempts
- timeout
- cancellation per session

Polling uses the registered subagent provider only. For FlowHunt flows, that
means the official invoke task protocol: submit once, store the task id, then
poll by `flow_id + task_id`.

Session cancellation honors the registered provider descriptor. If a provider
does not support cancellation, the runner does not call provider `cancel()`; it
marks the task terminal with a progress diagnostic so operations can see why no
remote cancellation was attempted.

## Terminal Events

Terminal task events are emitted exactly once:

- `subagent_task_completed`
- `subagent_task_failed`
- `subagent_task_timed_out`
- `subagent_task_cancelled`
- `subagent_task_late_completed`

The `terminal_event_emitted_at` marker is persisted with the task so a restarted
poller does not emit duplicate result events.

## Late Results

The runner accepts a `session_active(session_id)` callback. If a task completes
after the call/session ended, it emits `subagent_task_late_completed` instead of
normal completion. That lets the runtime store the result for transcript/audit
without trying to speak into a closed call.

## Provider Diagnostics

Polling exceptions are treated as retryable until the retry policy is exhausted.
After `max_attempts`, the task fails with a diagnostic error. Provider references
stay attached to the task for audit, including FlowHunt target ids and external
task ids.
