# Agent API

The voicebot core does not contain business logic. It emits call events and
waits for an external AI agent to answer asynchronously.

## Event Flow

1. Caller speaks.
2. STT produces a `user_transcript` event.
3. Voicebot emits `agent_response_requested`.
4. External agent reads pending tasks from `/agent/tasks`.
5. External agent posts an answer to `/calls/{call_id}/responses`.
6. Voicebot synthesizes that text and streams it back to the SIP call.

If the caller starts speaking while audio is playing, playback is interrupted
and the new turn becomes the next pending task.

Every event is also written to a per-call JSONL transcript under the service's
transcript directory. Docker stores this in the `voicebot-data` volume.

## HTTP API

Health:

```bash
curl http://127.0.0.1:8080/health
```

Read events:

```bash
curl 'http://127.0.0.1:8080/events?after=0'
```

Read pending agent tasks with compacted context:

```bash
curl http://127.0.0.1:8080/agent/tasks
```

Discover callable tools:

```bash
curl http://127.0.0.1:8080/agent/tools
```

Send an async answer:

```bash
curl -X POST http://127.0.0.1:8080/calls/CALL_ID/responses \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello, how can I help you?", "response_to_event_id":123}'
```

Read the full transcript for one call:

```bash
curl http://127.0.0.1:8080/calls/CALL_ID/transcript
```

Hang up a call:

```bash
curl -X POST http://127.0.0.1:8080/calls/CALL_ID/control \
  -H 'Content-Type: application/json' \
  -d '{"action":"hangup"}'
```

Transfer a call:

```bash
curl -X POST http://127.0.0.1:8080/calls/CALL_ID/control \
  -H 'Content-Type: application/json' \
  -d '{"action":"transfer", "target":"123456789"}'
```

The same actions are exposed as agent tools:

```bash
curl -X POST http://127.0.0.1:8080/agent/tools/hangup_call \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"call_id":"CALL_ID"}}'

curl -X POST http://127.0.0.1:8080/agent/tools/transfer_call \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"call_id":"CALL_ID", "target":"123456789"}}'
```

The control endpoint emits `call_control_requested` and
`call_control_completed` events so the agent can observe whether the operation
succeeded.

Watch live events:

```bash
websocat ws://127.0.0.1:8080/ws/events
```

## Local Command Agent

Use `agents/local_command_agent.py` to connect a local Codex-like command:

```bash
python agents/local_command_agent.py \
  --base-url http://127.0.0.1:8080 \
  --command 'codex exec -'
```

The command must read the prompt from stdin and write only the final spoken
answer to stdout. It may also write JSON with tool calls:

```json
{
  "say": "I will transfer you now.",
  "tool_calls": [
    {"name": "transfer_call", "arguments": {"call_id": "CALL_ID", "target": "123456789"}}
  ]
}
```

Complex agents can also call the control endpoints directly. For example, an
agent can decide to transfer a caller to a human, hang up abusive calls, or wait
for `call_ended` before writing post-call notes.

## Parallel Calls

The SIP/media layer accepts parallel incoming calls. Each call receives its own
AudioSocket session, `call_id`, in-memory state, and transcript file. The event
queue can contain tasks for multiple calls at the same time, so external agents
must use the `call_id` on each event/tool call.

The current local command agent processes tasks serially. Production agents
should either run workers per call or dispatch tasks by `call_id`. STT/TTS model
adapters should also be reviewed for provider-specific concurrency limits before
high call volume testing.

## Context Compaction

The service keeps recent events in memory and creates a simple rolling summary
when the event list grows too long. A stronger summarizer can run externally and
replace the summary:

```bash
curl -X POST http://127.0.0.1:8080/context/compact \
  -H 'Content-Type: application/json' \
  -d '{"summary":"Customer asked about pricing. Bot explained plans."}'
```
