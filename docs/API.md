# FlowHunt Voicebot API Reference

This document describes the HTTP and WebSocket API exposed by the `voicebot`
service. In Docker, the service listens on port `8080` inside the container and
is published to the host by `VOICEBOT_API_HOST_PORT`. The default local base URL
is:

```text
http://127.0.0.1:8080
```

All request and response bodies are JSON unless noted otherwise. API responses
that include secrets return redacted metadata instead of secret values.

## Common Objects

### Event

Most runtime activity is represented as an event:

```json
{
  "id": 123,
  "call_id": "call-abc",
  "type": "user_transcript",
  "timestamp": "2026-05-28T12:00:00.000000Z",
  "data": {}
}
```

Every event has:

- `id`: monotonic event ID.
- `call_id`: call/session identifier, or `system` for global context.
- `type`: event type. Use `GET /events/catalog` for the machine-readable list.
- `timestamp`: UTC timestamp.
- `data`: event-specific object.

### Pagination

Several endpoints accept:

- `after`: event ID cursor. Defaults to `0`.
- `limit`: maximum number of items. Defaults to `200`; hard maximum is `1000`.
- `call_id`: optional call filter where supported.

Invalid limits return `400`.

### Control Result

Asterisk/AMI operations return a control result:

```json
{
  "ok": true,
  "message": "Response: Success\r\n..."
}
```

If AMI is not configured, the field is usually `null`. If AMI is configured but
unreachable, `ok` is `false` and `message` describes the connection failure.

## Health And Runtime

### GET `/health`

Lightweight process health check.

Response:

```json
{
  "ok": true,
  "active_calls": ["call-abc"]
}
```

### GET `/health/readiness`

Structured readiness check for storage, AMI configuration, provider catalog, and
event catalog.

Response:

```json
{
  "ok": true,
  "active_calls": [],
  "checks": {
    "transcripts": {
      "ok": true,
      "message": "transcript directory is writable",
      "path": "/data/transcripts",
      "transcript_count": 0,
      "event_count": 0,
      "skipped_line_count": 0,
      "corrupt_transcript_count": 0,
      "corrupt_call_ids": []
    },
    "ami": {
      "ok": true,
      "message": "AMI control is configured",
      "configured": true,
      "host": "asterisk",
      "port": 5038,
      "username": "voicebot"
    },
    "providers": {
      "ok": true,
      "message": "provider catalog is populated",
      "supported": {
        "stt": [],
        "tts": [],
        "agent": []
      },
      "empty_groups": []
    },
    "event_catalog": {
      "ok": true,
      "message": "event catalog is valid",
      "missing_event_types": [],
      "integrity_issues": []
    }
  }
}
```

### GET `/config`

Returns redacted runtime configuration.

Response:

```json
{
  "settings": {
    "api_host": "0.0.0.0",
    "api_port": 8080,
    "openai_api_key": {
      "configured": true,
      "redacted": true
    },
    "ami_password": {
      "configured": true,
      "redacted": true
    }
  }
}
```

### GET `/providers`

Returns supported provider names for STT, TTS, and agent integrations.

Response:

```json
{
  "supported": {
    "stt": ["whisper", "openai"],
    "tts": ["supertonic", "openai"],
    "agent": ["openai-responses"]
  },
  "empty_groups": []
}
```

## Voicebot Admin

These prototype product-admin endpoints manage workspace-scoped voicebot
records in the local process store. FlowHunt production should back the same
contract with workspace-permission checks and database storage.

### GET `/workspaces/{workspace_id}/voicebots`

Lists voicebots in one workspace.

### POST `/workspaces/{workspace_id}/voicebots`

Creates a voicebot record.

Request:

```json
{
  "voicebot_id": "support-bot",
  "display_name": "Support bot",
  "enabled": true,
  "metadata": {"language": "en"}
}
```

### GET `/workspaces/{workspace_id}/voicebots/{voicebot_id}`

Returns one voicebot record.

### PATCH `/workspaces/{workspace_id}/voicebots/{voicebot_id}`

Updates mutable voicebot fields: `display_name`, `enabled`, and `metadata`.

### DELETE `/workspaces/{workspace_id}/voicebots/{voicebot_id}`

Deletes one voicebot record from the workspace.

### GET `/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels`

Lists SIP trunk, phone number, or WebRTC widget channel bindings assigned to
one voicebot.

### POST `/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels`

Creates a channel binding and makes it available to the runtime channel
resolver.

Request:

```json
{
  "channel_id": "support-sk-trunk",
  "kind": "sip_trunk",
  "external_id": "trunk-876",
  "enabled": true,
  "metadata": {"country": "sk"}
}
```

### GET `/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels/{channel_id}`

Returns one channel binding.

### PATCH `/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels/{channel_id}`

Updates mutable channel fields: `enabled` and `metadata`. Route identity fields
are immutable in this prototype; create a replacement channel to change
`kind`, `external_id`, workspace, or voicebot ownership.

### DELETE `/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels/{channel_id}`

Deletes one channel binding and removes it from runtime resolution.

### POST `/workspaces/{workspace_id}/voicebots/{voicebot_id}/validate`

Checks whether a voicebot is ready to run. The response includes `ok`,
channel counts, the normalized provider selection plan when provider config is
valid, and issue entries for missing or disabled voicebot records, missing or
disabled channels, and invalid or missing provider config.

## Provider Configuration

### PUT `/workspaces/{workspace_id}/voicebots/{voicebot_id}/providers`

Validates and saves workspace voicebot provider choices.

Request:

```json
{
  "stt": {"provider": "openai", "model": "gpt-4o-transcribe", "secret_ref": {"name": "openai-main"}},
  "tts": {"provider": "openai", "model": "gpt-4o-mini-tts", "secret_ref": {"name": "openai-main"}},
  "agent": {"provider": "openai-responses", "model": "gpt-4.1", "secret_ref": {"name": "openai-main"}}
}
```

### GET `/workspaces/{workspace_id}/voicebots/{voicebot_id}/providers`

Returns the saved provider config and normalized runtime selection plan.

## Voicebot Sessions

### GET `/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions`

Lists session records for one voicebot. Set `active_only=true` to return only
sessions whose status is still active.

### GET `/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}`

Returns one session record, scoped to the workspace and voicebot in the route.

### GET `/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}/timeline`

Returns in-memory event timeline entries for the session. Query parameters:
`after` skips already-seen event ids and `limit` defaults to 200.

### GET `/workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}/transcript`

Returns transcript events persisted for the session id. Query parameters:
`after` skips already-seen event ids and `limit` defaults to 200.

## Voicebot External Tasks

### GET `/workspaces/{workspace_id}/voicebots/{voicebot_id}/tasks`

Lists delegated subagent/external work for one voicebot. Optional query
parameters: `session_id` narrows the list to one call session and `status`
narrows it to a task lifecycle state such as `running`, `completed`, or
`failed`.

## Calls

### GET `/calls`

Lists active call sessions.

Response:

```json
{
  "calls": [
    {
      "call_id": "call-abc",
      "started_at": "2026-05-28T12:00:00.000000Z",
      "state": "connected"
    }
  ]
}
```

The exact call snapshot fields depend on active session state.

### GET `/calls/{call_id}`

Returns one active call snapshot.

Errors:

- `404`: active call not found.

### POST `/calls/{call_id}/responses`

Submits text from an external AI agent to be synthesized and played into an
active call.

Request:

```json
{
  "text": "Hello, how can I help you?",
  "response_to_event_id": 123
}
```

Fields:

- `text`: required text to synthesize.
- `response_to_event_id`: optional event ID this response answers. When present,
  the agent task tracker marks that task as responded.

Response:

```json
{
  "event": {
    "id": 124,
    "call_id": "call-abc",
    "type": "agent_response_received",
    "timestamp": "2026-05-28T12:00:01.000000Z",
    "data": {
      "text": "Hello, how can I help you?",
      "response_to_event_id": 123
    }
  }
}
```

Errors:

- `404`: active call not found.

### POST `/calls/{call_id}/control`

Requests an Asterisk call-control action.

Supported actions:

- `hangup`
- `transfer`
- `send_dtmf`

Request for hangup:

```json
{
  "action": "hangup",
  "response_to_event_id": 123
}
```

Request for transfer:

```json
{
  "action": "transfer",
  "target": "123456789",
  "response_to_event_id": 123
}
```

Request for DTMF:

```json
{
  "action": "send_dtmf",
  "digit": "1",
  "response_to_event_id": 123
}
```

Response:

```json
{
  "event": {
    "id": 130,
    "call_id": "call-abc",
    "type": "call_control_completed",
    "timestamp": "2026-05-28T12:00:02.000000Z",
    "data": {
      "action": "transfer",
      "ok": true,
      "message": "Response: Success\r\n...",
      "request_event_id": 129
    }
  }
}
```

Notes:

- The endpoint always emits `call_control_requested` before validation or
  execution.
- Successful or handled failures emit `call_control_completed`.

Errors:

- `400`: unsupported action, missing transfer target, invalid transfer target,
  missing DTMF digit, or invalid DTMF digit.
- `503`: Asterisk AMI is not configured.

### POST `/calls/{call_id}/playback/interrupt`

Stops queued or currently playing bot audio without ending the call.

Request:

```json
{
  "reason": "agent_requested",
  "response_to_event_id": 123
}
```

Response:

```json
{
  "event": {
    "id": 131,
    "call_id": "call-abc",
    "type": "bot_playback_interrupted",
    "timestamp": "2026-05-28T12:00:03.000000Z",
    "data": {
      "reason": "agent_requested"
    }
  }
}
```

Errors:

- `404`: active call not found.

## WebRTC Browser Calls

These endpoints create direct browser WebRTC calls. The browser sends
microphone audio to voicebot over WebRTC, and voicebot returns synthesized bot
audio as a remote audio track. After audio reaches the runtime, WebRTC calls use
the same VAD, STT, event, agent, TTS, playback, and transcript path as SIP calls.

For manual local testing, open:

```text
http://127.0.0.1:8080/webrtc/test
```

### GET `/webrtc/sessions`

Lists active WebRTC sessions.

Response:

```json
{
  "sessions": [
    {
      "session_id": "session-abc",
      "call_id": "webrtc-session-abc",
      "transport": "webrtc",
      "connection_state": "connected",
      "recording": false,
      "playback_active": false,
      "stopped": false,
      "active_turn": 1,
      "metadata": {
        "client": "browser-test"
      }
    }
  ]
}
```

Errors:

- `503`: WebRTC transport is not configured.

### POST `/webrtc/sessions`

Creates a WebRTC session from a browser SDP offer and returns the SDP answer.

Request:

```json
{
  "sdp": "v=0...",
  "type": "offer",
  "metadata": {
    "tenant_id": "tenant-1",
    "client": "browser"
  }
}
```

Fields:

- `sdp`: required SDP offer.
- `type`: must be `offer`.
- `metadata`: optional object copied into call lifecycle events and snapshots.

Response:

```json
{
  "session_id": "session-abc",
  "call_id": "webrtc-session-abc",
  "answer": {
    "sdp": "v=0...",
    "type": "answer"
  }
}
```

Errors:

- `400`: request type is not `offer`.
- `503`: WebRTC transport is not configured or `aiortc` is unavailable.

### DELETE `/webrtc/sessions/{session_id}`

Closes a WebRTC session and removes its active call from the registry.

Response:

```json
{
  "closed": true,
  "session_id": "session-abc"
}
```

Errors:

- `404`: WebRTC session not found.
- `503`: WebRTC transport is not configured.

### GET `/webrtc/test`

Returns a minimal browser test page. It uses `getUserMedia`, creates an
`RTCPeerConnection`, posts the SDP offer to `/webrtc/sessions`, and plays the
remote bot audio track in an `<audio>` element.

## Dynamic SIP Trunks

The voicebot service can manage many SIP trunks dynamically. It persists trunk
definitions to `VOICEBOT_SIP_TRUNK_REGISTRY_PATH`, renders the Asterisk include
file at `VOICEBOT_SIP_TRUNK_PJSIP_INCLUDE_PATH`, reloads PJSIP through AMI, and
asks Asterisk to register or unregister specific trunks.

Trunk IDs must contain only letters, numbers, underscores, or dashes and be at
most 64 characters.

### GET `/sip-trunks`

Lists configured trunks and current Asterisk registration output.

Response:

```json
{
  "trunks": [
    {
      "trunk_id": "customer-1",
      "host": "sip.example.com",
      "user": "customer_user",
      "display_name": "Customer 1",
      "enabled": true,
      "codecs": ["ulaw", "alaw", "slin"],
      "expiration": 300,
      "retry_interval": 30,
      "forbidden_retry_interval": 300,
      "registration": "trunk-customer-1-reg",
      "endpoint": "trunk-customer-1-endpoint",
      "password": {
        "configured": true,
        "redacted": true
      }
    }
  ],
  "registrations": {
    "ok": true,
    "message": "Response: Success\r\n..."
  }
}
```

Errors:

- `503`: SIP trunk registry is not configured.

### POST `/sip-trunks`

Creates or updates a SIP trunk. If `enabled` is true, the runtime writes the
trunk to the Asterisk include file, reloads PJSIP, and sends a register command
for that trunk.

Request:

```json
{
  "trunk_id": "customer-1",
  "host": "sip.example.com",
  "user": "customer_user",
  "password": "customer_password",
  "display_name": "Customer 1",
  "enabled": true,
  "codecs": ["ulaw", "alaw", "slin"],
  "expiration": 300,
  "retry_interval": 30,
  "forbidden_retry_interval": 300
}
```

Required fields:

- `trunk_id`
- `host`
- `user`
- `password` when `enabled` is true

Optional fields:

- `display_name`, default `""`
- `enabled`, default `true`
- `codecs`, default `["ulaw", "alaw", "slin"]`
- `expiration`, default `300`
- `retry_interval`, default `30`
- `forbidden_retry_interval`, default `300`

Response:

```json
{
  "trunk": {
    "trunk_id": "customer-1",
    "host": "sip.example.com",
    "user": "customer_user",
    "display_name": "Customer 1",
    "enabled": true,
    "password": {
      "configured": true,
      "redacted": true
    }
  },
  "reload": {
    "ok": true,
    "message": "Response: Success\r\n..."
  },
  "register": {
    "ok": true,
    "message": "Response: Success\r\n..."
  }
}
```

Errors:

- `400`: invalid trunk ID, missing required field, unsafe characters, invalid
  codec list, or invalid interval.
- `503`: SIP trunk registry is not configured.

### POST `/sip-trunks/{trunk_id}/connect`

Enables an existing trunk, regenerates the Asterisk include file, reloads PJSIP,
and sends a register command.

Response:

```json
{
  "trunk": {
    "trunk_id": "customer-1",
    "enabled": true,
    "password": {
      "configured": true,
      "redacted": true
    }
  },
  "reload": {
    "ok": true,
    "message": "Response: Success\r\n..."
  },
  "register": {
    "ok": true,
    "message": "Response: Success\r\n..."
  }
}
```

Errors:

- `400`: invalid trunk ID or invalid stored trunk data.
- `404`: trunk not found.
- `503`: SIP trunk registry is not configured.

### POST `/sip-trunks/{trunk_id}/disconnect`

Sends an unregister command for an enabled trunk, disables it in the registry,
regenerates the Asterisk include file, and reloads PJSIP.

Response:

```json
{
  "trunk": {
    "trunk_id": "customer-1",
    "enabled": false,
    "password": {
      "configured": true,
      "redacted": true
    }
  },
  "unregister": {
    "ok": true,
    "message": "Response: Success\r\n..."
  },
  "reload": {
    "ok": true,
    "message": "Response: Success\r\n..."
  }
}
```

If the trunk was already disabled, `unregister` is `null`.

Errors:

- `400`: invalid trunk ID or invalid stored trunk data.
- `404`: trunk not found.
- `503`: SIP trunk registry is not configured.

### DELETE `/sip-trunks/{trunk_id}`

Removes a trunk from the registry. If the trunk was enabled, the runtime sends
an unregister command before removal. It then regenerates the Asterisk include
file and reloads PJSIP.

Response:

```json
{
  "trunk": {
    "trunk_id": "customer-1",
    "enabled": true,
    "password": {
      "configured": true,
      "redacted": true
    }
  },
  "unregister": {
    "ok": true,
    "message": "Response: Success\r\n..."
  },
  "reload": {
    "ok": true,
    "message": "Response: Success\r\n..."
  }
}
```

Errors:

- `400`: invalid trunk ID.
- `404`: trunk not found.
- `503`: SIP trunk registry is not configured.

## Events And Context

### GET `/events`

Lists in-memory events.

Query parameters:

- `after`: event cursor, default `0`.
- `call_id`: optional call filter.
- `limit`: maximum events, default `200`.

Example:

```bash
curl 'http://127.0.0.1:8080/events?after=100&call_id=call-abc&limit=50'
```

Response:

```json
{
  "events": []
}
```

### GET `/events/catalog`

Returns the machine-readable event catalog.

Response:

```json
{
  "events": [
    {
      "type": "call_started",
      "category": "call_lifecycle",
      "agent_visible": true,
      "description": "..."
    }
  ],
  "integrity_issues": []
}
```

### GET `/context`

Returns compacted in-memory context.

Query parameters:

- `call_id`: optional call filter.

Response:

```json
{
  "events": [],
  "summary": null
}
```

The exact shape is produced by the event store and may include compacted summary
events when context has been compacted.

### POST `/context/compact`

Replaces long context with a summary event. This is intended for an external
compaction worker.

Request:

```json
{
  "summary": "The caller asked about billing. The bot confirmed identity.",
  "call_id": "call-abc"
}
```

Fields:

- `summary`: required summary text.
- `call_id`: optional, default `system`.

Response:

```json
{
  "event": {
    "id": 200,
    "call_id": "call-abc",
    "type": "context_compacted",
    "timestamp": "2026-05-28T12:00:00.000000Z",
    "data": {
      "summary": "The caller asked about billing. The bot confirmed identity."
    }
  }
}
```

## Agent Tasks

### GET `/agent/tasks`

Returns pending `agent_response_requested` events for active calls plus context.

Query parameters:

- `after`: event cursor, default `0`.
- `call_id`: optional call filter.
- `limit`: maximum pending tasks, default `200`.

Response:

```json
{
  "pending": [
    {
      "id": 123,
      "call_id": "call-abc",
      "type": "agent_response_requested",
      "timestamp": "2026-05-28T12:00:00.000000Z",
      "data": {
        "text": "I need help with my order."
      }
    }
  ],
  "context": {
    "events": []
  }
}
```

Only active-call tasks that have not been marked responded are returned.

### POST `/agent/tasks/claim`

Claims pending tasks before processing. Use this when multiple agent workers are
running.

Request:

```json
{
  "event_ids": [123, 124],
  "owner": "worker-1",
  "ttl_seconds": 60
}
```

Response:

```json
{
  "claimed_event_ids": [123],
  "owner": "worker-1"
}
```

Only active pending `agent_response_requested` events are claimable.

### POST `/agent/tasks/release`

Releases claimed tasks.

Request:

```json
{
  "event_ids": [123],
  "owner": "worker-1"
}
```

Response:

```json
{
  "released_event_ids": [123]
}
```

If `owner` is omitted, any owner claim may be released.

### POST `/agent/tasks/renew`

Renews active task claims.

Request:

```json
{
  "event_ids": [123],
  "owner": "worker-1",
  "ttl_seconds": 60
}
```

Response:

```json
{
  "renewed_event_ids": [123],
  "owner": "worker-1"
}
```

### GET `/agent/tasks/status`

Returns tracker state for responded tasks and active claims.

Query parameters:

- `owner`: optional claim owner filter.

Response:

```json
{
  "responded_event_ids": [123],
  "claims": []
}
```

### GET `/agent/tasks/summary`

Lists task events with derived state.

Query parameters:

- `after`: event cursor, default `0`.
- `call_id`: optional call filter.
- `owner`: optional claim owner filter.
- `limit`: maximum tasks, default `200`.

Response:

```json
{
  "tasks": [
    {
      "event": {
        "id": 123,
        "call_id": "call-abc",
        "type": "agent_response_requested",
        "timestamp": "2026-05-28T12:00:00.000000Z",
        "data": {}
      },
      "state": "pending"
    }
  ],
  "counts": {
    "pending": 1
  },
  "active_calls": ["call-abc"]
}
```

Possible task states include `pending`, `claimed`, `responded`, and `inactive`.

## Agent Tools

### GET `/agent/tools`

Returns legacy tool definitions.

Response:

```json
{
  "tools": [
    {
      "name": "say",
      "description": "Speak text into an active call.",
      "arguments": {
        "call_id": "Active call ID.",
        "text": "Text to synthesize and play."
      }
    }
  ]
}
```

### GET `/agent/tools/schema`

Returns JSON-schema function definitions suitable for tool-capable agents.

Response:

```json
{
  "tools": [
    {
      "type": "function",
      "name": "say",
      "description": "Speak text into an active call.",
      "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": false
      }
    }
  ]
}
```

### POST `/agent/tools/{tool_name}`

Executes one agent tool.

Request:

```json
{
  "arguments": {
    "call_id": "call-abc"
  }
}
```

Response depends on the selected tool. Unknown tools return `404`.

Supported tool names:

| Tool | Purpose |
| --- | --- |
| `say` | Submit text to speak into an active call. |
| `hangup_call` | Hang up an active call through Asterisk. |
| `transfer_call` | Transfer an active call to another extension or SIP target. |
| `send_dtmf` | Send one DTMF digit into an active call. |
| `stop_playback` | Stop queued or active bot playback. |
| `list_transcripts` | List call IDs with persisted transcripts. |
| `list_transcript_summaries` | List transcript metadata. |
| `get_transcript_stats` | Read aggregate transcript counts and corruption counters. |
| `get_transcript` | Read one call transcript. |
| `get_events` | Read recent in-memory events. |
| `get_metrics` | Read aggregated metrics. |
| `get_active_calls` | List active call IDs. |
| `get_call_state` | Read one active call state. |
| `get_runtime_config` | Read redacted runtime config. |
| `get_agent_task_status` | Read task tracker status. |
| `get_agent_task_summary` | Read agent task events with derived state. |

Tool argument details are available from `GET /agent/tools/schema`.

The bundled `openai-agent` and `anthropic-agent` workers both use this same tool
API. OpenAI tool schemas are returned directly from `/agent/tools/schema`; the
Anthropic worker converts those JSON-schema function definitions to Anthropic
Messages API tool definitions before calling the model.

## Transcripts

### GET `/calls/{call_id}/transcript`

Reads persisted transcript events for one call.

Query parameters:

- `after`: event cursor, default `0`.
- `limit`: maximum events, default `200`.

Response:

```json
{
  "call_id": "call-abc",
  "events": [
    {
      "id": 1,
      "call_id": "call-abc",
      "type": "call_started",
      "timestamp": "2026-05-28T12:00:00.000000Z",
      "data": {}
    }
  ]
}
```

### GET `/transcripts`

Lists call IDs with persisted transcript files.

Response:

```json
{
  "call_ids": ["call-abc"]
}
```

### GET `/transcripts/summary`

Lists transcript summaries.

Query parameters:

- `after_call_id`: optional call ID cursor.
- `limit`: maximum summaries, default `200`.

Response:

```json
{
  "transcripts": [
    {
      "call_id": "call-abc",
      "event_count": 10,
      "first_timestamp": "2026-05-28T12:00:00.000000Z",
      "last_timestamp": "2026-05-28T12:05:00.000000Z",
      "skipped_line_count": 0,
      "corrupt": false
    }
  ]
}
```

### GET `/transcripts/stats`

Returns aggregate transcript storage statistics.

Query parameters:

- `after_call_id`: optional call ID cursor.
- `limit`: maximum summaries to aggregate, default `200`.

Response:

```json
{
  "transcript_count": 1,
  "event_count": 10,
  "skipped_line_count": 0,
  "corrupt_transcript_count": 0,
  "corrupt_call_ids": []
}
```

## Metrics

### GET `/metrics`

Returns aggregated timing and operational metrics from recent in-memory events.

Query parameters:

- `call_id`: optional call filter.

Response:

```json
{
  "count": 0,
  "metrics": {}
}
```

The exact metrics depend on emitted `metrics` events.

## Observability

### GET `/observability/timeline`

Returns a categorized event timeline for debugging a call or workspace slice.
The response includes event category counts, audio health counters, and provider
latency/failure rollups.

Query parameters:

- `after`: optional event ID cursor.
- `call_id`: optional call filter.
- `workspace_id`: optional workspace filter.
- `voicebot_id`: optional voicebot filter.
- `session_id`: optional session filter.
- `limit`: maximum events, default `1000`.

### POST `/observability/evaluate`

Runs deterministic conversation checks against selected events.

Request:

```json
{
  "call_id": "call-1",
  "must_include_event_types": ["call_connected", "user_transcript"],
  "max_duplicate_agent_responses": 1,
  "require_final_agent_response": true
}
```

## Scaling

### GET `/scaling/topology`

Returns worker roles, queue names, concurrency, shared state, and event bus
settings for the voicebot runtime.

### POST `/scaling/workload-plan`

Builds a routing and capacity plan for a workspace voicebot workload.

Request:

```json
{
  "workspace_id": "workspace-1",
  "voicebot_id": "voicebot-1",
  "concurrent_sessions": 50,
  "session_id": "session-1",
  "stt_provider": "openai",
  "tts_provider": "openai",
  "agent_provider": "anthropic"
}
```

### POST `/scaling/workers/heartbeat`

Records or refreshes a worker presence record.

```json
{
  "worker_id": "agent-1",
  "role": "agent_worker",
  "queue": "voicebot.agent",
  "workspace_id": "workspace-1",
  "voicebot_id": "voicebot-1",
  "capacity": 3,
  "status": "active"
}
```

### GET `/scaling/workers`

Lists active workers. Optional query filters: `role`, `workspace_id`.

### GET `/scaling/capacity`

Summarizes active worker capacity by role. Optional query filter:
`workspace_id`.

### POST `/scaling/workers/{worker_id}/drain`

Marks a worker as draining so it no longer appears in active worker listings.

### DELETE `/scaling/workers/{worker_id}`

Removes a worker presence record.

## Multimodal Context

### GET `/calls/{call_id}/multimodal`

Returns normalized multimodal content parts attached to a call.

### POST `/calls/{call_id}/multimodal/parts`

Attaches one normalized content part and emits `multimodal_content_added`.

Request:

```json
{
  "modality": "image",
  "direction": "input",
  "mime_type": "image/png",
  "uri": "s3://workspace/file.png",
  "workspace_id": "workspace-1",
  "metadata": {"source": "browser"}
}
```

## API Surface

### GET `/api/surface`

Returns the FlowHunt API surface catalog grouped by area, plus a boolean showing
whether all public endpoints are workspace-scoped, summary counts by area,
visibility, and scope source, scope violations, and catalog integrity issues.

### GET `/api/surface/prototypes`

Returns prototype-only endpoints that should not be exposed as public product
APIs.

## WebSocket

### WS `/ws/events`

Streams events to WebSocket clients. After connection, the server sends event
JSON objects in order. The connection remains open until the client disconnects.

Example:

```bash
websocat ws://127.0.0.1:8080/ws/events
```

Message:

```json
{
  "id": 123,
  "call_id": "call-abc",
  "type": "user_transcript",
  "timestamp": "2026-05-28T12:00:00.000000Z",
  "data": {
    "text": "Hello"
  }
}
```

## Error Format

FastAPI returns errors in the standard shape:

```json
{
  "detail": "transfer requires target"
}
```

Validation errors from malformed request bodies return FastAPI/Pydantic
validation details in `detail`.

## Security Notes

- Do not expose this API directly to the public internet without authentication,
  authorization, request limits, and tenant isolation.
- SIP trunk passwords are stored in the runtime trunk registry and rendered into
  the Asterisk include file. Both paths must be private runtime storage.
- API responses redact configured passwords and API keys, but command-line tools
  and Compose config rendering can still reveal local environment variables.
