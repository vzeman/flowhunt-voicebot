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

The first adapter is `FlowHuntSubagentProvider`, with provider kinds:

- `flowhunt_flow`
- `flowhunt_project`

For FlowHunt flow execution, the adapter uses the invoke task protocol:

1. submit with flow invoke
2. store the returned task id
3. poll the task by `flow_id + task_id`
4. complete only when the provider task returns a final result/status

## Next Integration Step

The current implementation is intentionally independent from HTTP routes and
live calls. Next steps:

- persist subagent tasks in durable storage
- expose task APIs
- connect communication agent tool calls to `SubagentCoordinator`
- emit lifecycle events for task state changes
- throttle progress updates so the voice agent does not repeat itself
