# Agent API

The voicebot core does not contain business logic. It emits call events and
waits for an external AI agent to answer asynchronously.

## Event Flow

1. Asterisk connects a call and the service emits `call_started` and
   `call_connected`.
2. If `VOICEBOT_GREET_ON_CONNECT=true`, voicebot emits
   `agent_response_requested` with `reason=call_connected` so the agent can
   greet the caller immediately.
3. Caller speaks.
4. STT produces a `user_transcript` event.
5. Voicebot emits `agent_response_requested` with the recognized text.
6. External agent reads pending tasks from `/agent/tasks`.
7. External agent posts an answer or calls a tool.
8. Voicebot synthesizes spoken responses and streams them back to the SIP call.

If the caller starts speaking while audio is playing, playback is interrupted
and the new turn becomes the next pending task.

Every event is also written to a per-call JSONL transcript under the service's
transcript directory. Docker stores this in the `voicebot-data` volume.

## Event Catalog

Call lifecycle:

- `call_started`: AudioSocket session was created and call ID is known.
- `call_connected`: Call media is connected and the agent may greet the caller.
- `call_ended`: AudioSocket session ended.

Caller media and STT:

- `user_speech_started`: VAD detected caller speech.
- `user_speech_finished`: VAD detected end of caller speech.
- `stt_started`: STT started for a speech turn.
- `stt_finished`: STT finished and recognized text.
- `stt_no_text`: STT finished without usable text.
- `user_transcript_partial`: Partial recognized caller text from streaming STT.
- `user_transcript`: Recognized caller text.
- `dtmf`: Caller sent a DTMF digit.

Agent tasks and responses:

- `agent_response_requested`: Agent should decide what to do. Reasons include
  `call_connected` and caller transcript turns.
- `agent_response_partial`: Partial text response from a streaming agent.
- `agent_response_received`: Service received a response from an agent.
- `agent_response_dropped`: Response was intentionally not played, usually
  because the caller was already speaking.
- `agent_response_queued`: Response audio was queued for playback.

TTS and playback:

- `tts_started`: TTS synthesis started.
- `tts_finished`: TTS synthesis finished.
- `tts_failed`: TTS synthesis failed.
- `bot_playback_started`: Bot audio started playing to the call.
- `bot_playback_interrupted`: Playback was stopped because caller speech won.
- `bot_playback_finished`: Queued bot audio finished playing.

Control and context:

- `call_control_requested`: Agent or API requested a call control action.
- `call_control_completed`: Asterisk returned a result for the control action.
- `context_compacted`: Long event context was summarized.
- `metrics`: Timing or operational metric emitted by the runtime.
- `system`: Operational fallback event for unexpected or low-level conditions.

New provider-specific events should be additive. Agents should ignore event
types they do not understand and rely on `call_id`, `type`, `timestamp`, and
`data` being present on every event.

## HTTP API

Health:

```bash
curl http://127.0.0.1:8080/health
```

List active call state:

```bash
curl http://127.0.0.1:8080/calls
curl http://127.0.0.1:8080/calls/CALL_ID
```

Read events:

```bash
curl 'http://127.0.0.1:8080/events?after=0'
```

Read the machine-readable event catalog:

```bash
curl http://127.0.0.1:8080/events/catalog
```

Read pending agent tasks with compacted context:

```bash
curl http://127.0.0.1:8080/agent/tasks
```

For parallel call handling, filter tasks to one call or bound each poll:

```bash
curl 'http://127.0.0.1:8080/agent/tasks?call_id=CALL_ID&limit=10'
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

List persisted transcript call IDs:

```bash
curl http://127.0.0.1:8080/transcripts
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

Send one DTMF digit:

```bash
curl -X POST http://127.0.0.1:8080/calls/CALL_ID/control \
  -H 'Content-Type: application/json' \
  -d '{"action":"send_dtmf", "digit":"1"}'
```

The same actions are exposed as agent tools:

```bash
curl -X POST http://127.0.0.1:8080/agent/tools/hangup_call \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"call_id":"CALL_ID"}}'

curl -X POST http://127.0.0.1:8080/agent/tools/transfer_call \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"call_id":"CALL_ID", "target":"123456789"}}'

curl -X POST http://127.0.0.1:8080/agent/tools/send_dtmf \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"call_id":"CALL_ID", "digit":"1"}}'
```

Stop currently queued or playing bot audio without ending the call:

```bash
curl -X POST http://127.0.0.1:8080/agent/tools/stop_playback \
  -H 'Content-Type: application/json' \
  -d '{"arguments":{"call_id":"CALL_ID", "reason":"agent_requested"}}'
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
