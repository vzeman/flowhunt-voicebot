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

Workspace-scoped product endpoints under `/workspaces/{workspace_id}/...` can
be guarded by an internal allow-list while the service is embedded into
FlowHunt. Set `VOICEBOT_WORKSPACE_ACCESS_CONTROL_ENABLED=true` and
`VOICEBOT_ALLOWED_WORKSPACE_IDS=workspace-1,workspace-2` to reject other
workspace IDs with `403`.

## API Surface Audiences

The local Docker runtime still serves a combined FastAPI app for development,
but every HTTP route is now classified by audience:

- `public`: caller-safe runtime endpoints that may be exposed through public
  ingress. Current examples are public health checks and WebRTC session offer
  creation.
- `internal`: FlowHunt backend, worker, agent, dashboard, diagnostics,
  call-control, storage, transcript, event, and admin endpoints. These must not
  be exposed to public ingress.
- `local_dev`: local developer tooling such as the browser WebRTC test page.

Generated OpenAPI specs are split by audience:

- `GET /openapi/public.json`
- `GET /openapi/internal.json`

The public OpenAPI spec excludes internal routes and local developer tools. The
internal spec includes internal routes and local developer tools for the current
Docker workflow, but it excludes public-only session creation operations. Later
production work will bind these audiences to separate ingress/services and add
internal service authentication.

`GET /api/surface` also returns a route-audience inventory so tests and
operators can detect unclassified endpoints before deployment.

## Internal Service Authentication

Internal and `local_dev` routes can be protected with a service API key:

```text
VOICEBOT_INTERNAL_AUTH_ENABLED=true
VOICEBOT_INTERNAL_AUTH_HEADER=X-FlowHunt-Internal-Key
VOICEBOT_INTERNAL_API_KEYS=admin:control-plane:secret-value:internal:*
```

`VOICEBOT_INTERNAL_API_KEYS` is comma-separated. Supported entry formats are:

- `secret`
- `key_id:secret`
- `key_id:service:secret:scope1|scope2`

When enabled, internal endpoints reject requests without a valid key. Public
caller-safe endpoints such as `GET /health`, `GET /health/liveness`, and public
WebRTC offer creation do not require this internal service key. Key values are
redacted from `/config` and auth audit events include only key metadata such as
`key_id`, service, scope, method, and path.

Public connection limits are configured separately:

```text
VOICEBOT_PUBLIC_SESSION_RATE_LIMIT_PER_MINUTE=60
VOICEBOT_PUBLIC_VOICEBOT_MAX_CONCURRENT_SESSIONS=100
VOICEBOT_PUBLIC_SDP_MAX_BYTES=131072
```

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
    },
    "durable_storage": {
      "ok": true,
      "message": "durable storage is reachable",
      "stores": {
        "events": {
          "kind": "JsonEventStore",
          "path": "/data/events/events.jsonl",
          "load_diagnostics": {
            "loaded_events": 0,
            "skipped_malformed_json": 0
          },
          "warning_count": 0,
          "writable": true
        }
      },
      "unwritable": [],
      "warning_counts": {}
    }
  }
}
```

### GET `/storage/drivers`

Lists the storage driver registry and the currently selected driver per storage
family. This endpoint is operational metadata only; it does not expose stored
records or secrets. Family-level aliases such as
`VOICEBOT_RELATIONAL_STORE_PROVIDER`, `VOICEBOT_CACHE_STORE_PROVIDER`, and
`VOICEBOT_AUDIO_ARTIFACT_STORE_PROVIDER` are reflected in the selected driver
payload, and backend URLs are redacted.

Response:

```json
{
  "registry": {
    "families": {
      "events": [
        {
          "family": "events",
          "driver": "jsonl",
          "scope": "node",
          "managed": false,
          "supports_local_dev": true,
          "supports_production": false,
          "consistency": "append-only JSONL event log",
          "idempotency_fields": [],
          "required_scope_fields": [],
          "notes": ""
        }
      ]
    }
  },
  "selected": {
    "events": {
      "family": "events",
      "driver": "jsonl",
      "configured_driver": "json",
      "path": "/data/events/events.jsonl"
    },
    "audio_artifacts": {
      "family": "audio_artifacts",
      "driver": "filesystem",
      "configured_driver": "filesystem",
      "path": "/data/tts-cache"
    }
  }
}
```

### GET `/storage/contracts`

Lists the storage contract catalog: required scope fields, idempotency fields,
local providers, and intended production backend classes for every storage
family.

### GET `/health/readiness/roles`

Returns readiness grouped by enabled deployment role from
`VOICEBOT_RUNTIME_ROLES`. This endpoint is for local split-role testing and
future Kubernetes probes. The response includes a `routing` section that marks
whether public WebRTC, internal API, SIP media, and worker-queue routing are
safe for the selected role set.

### GET `/deployment/topology`

Returns the deployment role catalog, local Docker mapping, future Kubernetes
deployment names, target services, ingress boundaries, port matrix, role
queues, readiness checks, startup probe hints, and resource profiles. No
Kubernetes manifests are generated by this endpoint.

### GET `/dashboard`

Returns the internal operations dashboard HTML shell. This route is internal
only and must be exposed only through private dashboard ingress. If
`VOICEBOT_DASHBOARD_AUTH_ENABLED=true`, a dashboard user identity is required;
an internal service API key alone is not a dashboard login.

### GET `/dashboard/state`

Returns dashboard state for the selected workspace: voicebot cards, channels,
public routes, active sessions, and recent events. This route is internal only
and requires internal auth when `VOICEBOT_INTERNAL_AUTH_ENABLED=true`. Dashboard
user auth filters the workspace list and rejects requests for unauthorized
workspaces.

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

### GET `/security/contract`

Returns the active workspace-isolation, audit, secret-redaction, retention, and
network-policy contract. This is an internal diagnostic endpoint.

Response:

```json
{
  "contract": {
    "mode": "local_permissive",
    "workspace_access": {
      "enabled": false,
      "mandatory_outside_local": true,
      "production_ready": true
    },
    "secret_handling": {
      "raw_secret_api_responses": false
    },
    "audit": {
      "event_type": "security_audit"
    }
  },
  "issues": []
}
```

### GET `/workspaces/{workspace_id}/security/retention`

Returns retention classes and deletion hooks for events, transcripts,
recordings, cached TTS audio, and delegated task state. Workspace access is
checked before returning the policy.

### POST `/workspaces/{workspace_id}/security/retention/delete`

Plans or requests retention deletion hooks for a workspace scope. The request
can target narrower `voicebot_id`, `session_id`, `call_id`, `artifact_id`, and
retention `classes`. `dry_run=true` returns the hooks without deleting data;
`dry_run=false` records the request for the configured storage drivers.

Request:

```json
{
  "voicebot_id": "voicebot-1",
  "session_id": "session-1",
  "classes": ["events", "transcripts", "recordings"],
  "reason": "user_erasure_request",
  "dry_run": true
}
```

The endpoint emits a redacted `security_audit` event with action
`retention_delete`.

### POST `/workspaces/{workspace_id}/security/audit`

Emits a redacted `security_audit` event for a workspace-scoped sensitive action.

Request:

```json
{
  "action": "provider_config_change",
  "actor": "flowhunt-api",
  "voicebot_id": "voicebot-1",
  "resource_type": "provider_config",
  "resource_id": "voicebot-1",
  "outcome": "saved",
  "metadata": {
    "api_key": "never returned raw"
  }
}
```

Response contains the emitted event. Sensitive metadata fields are returned as
redacted `{ "configured": true, "redacted": true }` objects.

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
records in the local process store. The route workspace is checked by the
workspace access policy before records, channels, sessions, tasks, or provider
config are read or mutated.

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

### GET `/workspaces/{workspace_id}/voicebots/{voicebot_id}/public-routes`

Lists public host/path routes assigned to a voicebot. A public route maps an
incoming public URL to a workspace voicebot channel without requiring one port
per voicebot.

### POST `/workspaces/{workspace_id}/voicebots/{voicebot_id}/public-routes`

Creates a public route for an existing channel.

Request:

```json
{
  "route_id": "support-public",
  "channel_id": "support-widget",
  "host": "voice.example.com",
  "path_prefix": "/support",
  "status": "active",
  "tls_mode": "managed",
  "allowed_origins": ["https://www.example.com"],
  "metadata": {"environment": "production"}
}
```

Fields:

- `route_id`: workspace-unique route identifier.
- `channel_id`: existing voicebot channel receiving public sessions.
- `host`: public host or custom domain. Port and URL scheme are normalized away.
- `path_prefix`: optional path prefix. The resolver uses longest-prefix match.
- `status`: `pending`, `active`, or `disabled`. Only active routes resolve
  public sessions.
- `tls_mode`: `managed` or `custom`; certificate provisioning is handled by the
  production ingress/certificate layer.
- `allowed_origins`: origin allow-list for the future website widget/public
  connection layer. If the list is non-empty, public WebRTC offers must include
  a matching `Origin` header.

Duplicate active `host + path_prefix` routes are rejected, even across
workspaces, because production ingress must have one unambiguous destination.

### PATCH `/workspaces/{workspace_id}/voicebots/{voicebot_id}/public-routes/{route_id}`

Updates mutable public route fields. Workspace and voicebot ownership are
immutable. If `channel_id` is changed, the replacement channel must already
belong to the same workspace voicebot.

### DELETE `/workspaces/{workspace_id}/voicebots/{voicebot_id}/public-routes/{route_id}`

Deletes one public route.

### POST `/workspaces/{workspace_id}/voicebots/{voicebot_id}/validate`

Checks whether a voicebot is ready to run. The response includes `ok`,
channel counts, the normalized provider selection plan when provider config is
valid, and issue entries for missing or disabled voicebot records, missing or
disabled channels, and invalid or missing provider config.

## Provider Configuration

## Prompt Configuration

Each voicebot can override the default prompts used by the live communication
agent and STT layer. Prompt config is workspace-scoped and cached in the runtime
so `/agent/tasks` can include it with pending work instead of requiring the
agent worker to make another API call per turn.

### GET `/workspaces/{workspace_id}/voicebots/{voicebot_id}/prompts`

Returns the effective prompt config and its source: `prompt_override`,
`runtime_config`, or `default`.

Response:

```json
{
  "workspace_id": "workspace-1",
  "voicebot_id": "support-sk",
  "source": "prompt_override",
  "prompts": {
    "greeting": "Pozdrav volajuceho po slovensky.",
    "system_prompt": "Use concise Slovak.",
    "stt_prompt": "LiveAgent FlowHunt",
    "language": "sk"
  }
}
```

### PUT `/workspaces/{workspace_id}/voicebots/{voicebot_id}/prompts`

Replaces the full prompt config. All fields have defaults, but callers should
send every field when replacing the config.

### PATCH `/workspaces/{workspace_id}/voicebots/{voicebot_id}/prompts`

Updates only the supplied prompt fields. Supported fields are `greeting`,
`system_prompt`, `stt_prompt`, and `language`.

Use `language: "auto"` for multilingual voicebots. In that mode the default STT
adapter does not force a language hint, and the communication agent is
instructed to answer in the caller's detected language. Use a concrete language
code such as `sk` or `en` when the voicebot should prefer that language for
greeting and responses.

When the runtime detects a language from accepted caller transcripts, it keeps
that language in session context and includes it in `/agent/tasks` as
`session_language`, `session_languages_by_call_id`, and, for `auto` prompt
configs, an effective prompt language with `language_source:
"session_detected"`. Dropped or stale STT results do not update the session
language.

Prompt changes emit `voicebot_prompts_updated` and a `security_audit` event.

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

## Transport Capabilities

### GET `/workspaces/{workspace_id}/voicebots/{voicebot_id}/transports`

Returns SIP/WebRTC transport capabilities that the voicebot runtime can expose
for a workspace voicebot, including supported call-control actions,
modalities, sample-rate requirements, and playback interruption support.

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

### GET `/subagent/providers`

Internal runtime endpoint that returns registered provider-neutral colleague
task adapters and descriptor metadata such as async polling support,
cancellation support, required metadata, and clean/raw result context.

### POST `/subagent/tasks`

Submits provider-neutral delegated work to any registered subagent provider.

Request:

```json
{
  "workspace_id": "workspace-1",
  "voicebot_id": "voicebot-1",
  "session_id": "session-1",
  "request_event_id": 123,
  "provider": "flowhunt_flow",
  "input_text": "Count the pages in this sitemap.",
  "dedupe_key": "session-1:turn-123",
  "metadata": {},
  "schedule": true
}
```

For FlowHunt providers, flow/project target IDs are taken from the registered
integration configuration. They are not accepted from communication agent tool
calls and are ignored as task metadata for target selection.

Per-provider subagent prompt hooks can be configured in versioned runtime
config under `subagents.prompts`. Keys are provider kinds such as
`flowhunt_flow`, `flowhunt_project`, or `internal_worker`.

```json
{
  "subagents": {
    "prompts": {
      "flowhunt_flow": {
        "before_call_prompt": "I will ask the specialist now.",
        "after_call_prompt": "The specialist is checking it now.",
        "result_prompt": "Use this colleague result for the caller: {result}"
      }
    }
  }
}
```

`before_call_prompt` is the immediate spoken acknowledgement before a subagent
is called. `after_call_prompt` is stored with the task and used as the provider
progress message after submission. `result_prompt` is rendered when the task
finishes and supports `{result}`, `{provider}`, `{status}`, `{error}`,
`{call_id}`, `{task_id}`, `{external_task_id}`, and `{input_text}`. When a
custom result prompt exists, the communication agent receives it as
`consume_prompt` and uses the model path to turn the subagent result into a
customer-facing spoken answer.

### POST `/subagent/tasks/speculative`

Starts cancellable delegated work from stable partial intent. The task is marked
speculative and its completed result is not sent to the communication agent
until confirmed.

```json
{
  "workspace_id": "workspace-1",
  "voicebot_id": "voicebot-1",
  "session_id": "session-1",
  "request_event_id": 122,
  "provider": "flowhunt_flow",
  "input_text": "Count pages in the sitemap",
  "speculative_key": "session-1:turn-7",
  "metadata": {}
}
```

### POST `/subagent/tasks/{task_id}/confirm-speculative`

Confirms speculative work after final STT matches the intent.

```json
{
  "workspace_id": "workspace-1",
  "final_request_event_id": 123,
  "final_input_text": "Count the pages in this sitemap.",
  "notify_if_terminal": true
}
```

### POST `/subagent/tasks/{task_id}/cancel-speculative`

Cancels speculative work when final STT changes the request.

```json
{
  "workspace_id": "workspace-1",
  "reason": "final_transcript_changed"
}
```

### POST `/subagent/tasks/{task_id}/cancel`

Cancels a delegated task in the supplied workspace and emits its terminal task
event once.

Request:

```json
{
  "workspace_id": "workspace-1"
}
```

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

### GET `/calls/state-store`

Internal runtime diagnostics endpoint. Returns persisted call snapshots from the
configured call-state store. Set `active_only=true` to return only snapshots
whose last stored state is `active`.

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
  "response_to_event_id": 123,
  "response_kind": "direct_answer",
  "partial": false,
  "finalize_only": false
}
```

Fields:

- `text`: required text to synthesize.
- `response_to_event_id`: optional event ID this response answers. When present,
  the agent task tracker marks that task as responded after a final response.
- `response_kind`: optional label such as `direct_answer`, `progress_ack`,
  `stream_chunk`, or `stream_finalized`.
- `partial`: optional boolean. When `true`, the text is spoken and persisted as
  `agent_response_partial`, but the task is not marked responded.
- `finalize_only`: optional boolean. When `true`, no audio is synthesized; the
  task identified by `response_to_event_id` is marked responded and a final
  stream marker is recorded. This is used after all streaming chunks were sent.

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

Streaming agents should send one or more partial chunks:

```json
{
  "text": "I can check that.",
  "response_to_event_id": 123,
  "response_kind": "stream_chunk",
  "partial": true
}
```

Then finalize the task:

```json
{
  "text": "",
  "response_to_event_id": 123,
  "response_kind": "stream_finalized",
  "finalize_only": true
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

### GET `/.well-known/flowhunt-voicebot`

Returns caller-safe bootstrap metadata for the public route resolved from
`Host`/`X-Forwarded-Host` and the forwarded path prefix. Website widgets should
call this endpoint before creating a WebRTC offer so they can discover the
resolved voicebot, session endpoint, ICE servers, and public admission limits.

Response:

```json
{
  "route_id": "support-public",
  "workspace_id": "workspace-1",
  "voicebot_id": "support-bot",
  "channel_id": "support-widget",
  "display_name": "Support bot",
  "transport": "webrtc",
  "session_endpoint": "/webrtc/sessions",
  "widget_script": "/widget.js",
  "widget_page": "/widget",
  "widget": {
    "enabled": true,
    "display_name": "Support bot",
    "launcher_label": "Talk to support",
    "welcome_label": "Voice call",
    "locale": "en",
    "theme": {
      "primary_color": "#0969da",
      "placement": "bottom-right"
    },
    "show_captions": false,
    "visitor_metadata_max_bytes": 2048,
    "recording_visible_to_visitor": false
  },
  "ice_servers": ["stun:stun.l.google.com:19302"],
  "modalities": {"input": ["audio"], "output": ["audio"]},
  "limits": {
    "sdp_max_bytes": 131072,
    "rate_limit_per_minute": 60,
    "max_concurrent_sessions": 100
  }
}
```

Errors:

- `404`: no active public route matches the forwarded host/path.
- `403`: route, target voicebot, or target channel is disabled.

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
  On public routes this is treated as untrusted visitor metadata, limited to
  2048 bytes, stripped of reserved routing keys, and stored under
  `visitor_metadata`.

When a production ingress forwards a public custom URL to this endpoint, the
runtime can resolve the voicebot from `Host`/`X-Forwarded-Host` and
`X-Forwarded-Prefix`, `X-Original-URI`, or `X-Forwarded-URI`. A resolved public
route adds `workspace_id`, `voicebot_id`, `channel_id`, `public_route_id`,
`public_route_host`, and `public_route_path_prefix` to the session metadata.
The route must be active, the target voicebot must be enabled, and the target
channel must be enabled.

For resolved public routes, session admission is checked before the WebRTC
session is created:

- The SDP offer must fit within `VOICEBOT_PUBLIC_SDP_MAX_BYTES`.
- If the route has `allowed_origins`, the request `Origin` must match one of
  them after trailing-slash normalization.
- A voicebot cannot exceed
  `VOICEBOT_PUBLIC_VOICEBOT_MAX_CONCURRENT_SESSIONS` active WebRTC sessions.
- Each public route has a fixed-window session creation rate limit controlled
  by `VOICEBOT_PUBLIC_SESSION_RATE_LIMIT_PER_MINUTE`.

Every admission accept or reject writes a `session_admission_decided` event with
the resolved route data, decision, and reason.

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
- `403`: public route origin is not allowed.
- `413`: public route SDP offer exceeds the configured maximum size, or public
  visitor metadata exceeds the configured maximum.
- `429`: public route rate or concurrent-session limit is exceeded.
- `503`: WebRTC transport is not configured or `aiortc` is unavailable.

### DELETE `/webrtc/sessions/{session_id}`

Closes a WebRTC session and removes its active call from the registry.
This endpoint is caller-safe so an embedded widget can end its own browser
session without receiving an internal API key.

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

After the call ends, the page checks `/calls/{call_id}/recording` and shows a
call-recording `<audio>` element when a speech-only recording is available.

### GET `/widget.js`

Returns the public embeddable browser widget JavaScript. It calls only public
runtime endpoints: `/.well-known/flowhunt-voicebot`, `POST /webrtc/sessions`,
and `DELETE /webrtc/sessions/{session_id}`. It does not call internal prompts,
provider configuration, task queues, event logs, diagnostics, or dashboard
APIs.

Example installation:

```html
<script src="https://voice.example.com/widget.js" async></script>
```

Optional attributes:

- `data-inline="true"` renders inline instead of a floating bottom-right button.
- `data-visitor-metadata='{"plan":"trial"}'` sends bounded caller metadata with
  the WebRTC session.

### GET `/widget`

Returns a minimal full-page/direct-link wrapper around `widget.js`.

## Call Recordings

Speech-only recordings are created at call end when
`VOICEBOT_CALL_RECORDING_ENABLED=true`. The recording WAV concatenates captured
caller and voicebot speech segments and omits silence. Metadata keeps the
original call offsets for each segment so the compact playback can still be
mapped back to the realtime conversation.

### GET `/calls/{call_id}/recording`

Returns metadata for the saved speech-only recording.

Response:

```json
{
  "artifact_id": "webrtc-session-1.speech.wav",
  "metadata": {
    "call_id": "webrtc-session-1",
    "kind": "speech_only_call_recording",
    "sample_rate": 16000,
    "segment_count": 2,
    "duration_seconds": 3.42,
    "original_voice_span_seconds": 8.91,
    "silence_removed": true,
    "segments": [
      {
        "source": "caller",
        "start_seconds": 1.22,
        "end_seconds": 2.14,
        "duration_seconds": 0.92,
        "playback_start_seconds": 0.0,
        "sample_rate": 16000,
        "samples": 14720,
        "metadata": {"turn_id": 1}
      }
    ]
  }
}
```

Errors:

- `404`: recording not found for this call.
- `503`: audio artifact storage is not configured.

### GET `/calls/{call_id}/recording.wav`

Returns the speech-only recording as `audio/wav`.

Errors:

- `404`: recording not found for this call.
- `503`: audio artifact storage is not configured.

## Dynamic SIP Trunks

The voicebot service can manage many SIP trunks dynamically. It persists trunk
definitions to `VOICEBOT_SIP_TRUNK_REGISTRY_PATH`, renders the Asterisk include
file at `VOICEBOT_SIP_TRUNK_PJSIP_INCLUDE_PATH`, reloads PJSIP through AMI, and
asks Asterisk to register or unregister specific trunks.

Trunk IDs must contain only letters, numbers, underscores, or dashes and be at
most 64 characters.

This API manages runtime dynamic trunks only. The local-development startup seed
from `SIP_HOST`, `SIP_USER`, and `SIP_PASSWORD` is generated by the Asterisk
entrypoint when the dynamic include file is missing or empty, and it is not
stored in `VOICEBOT_SIP_TRUNK_REGISTRY_PATH` or returned here unless it is also
created through this API.

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
      "auth_user": "customer_user",
      "contact_user": "customer_user",
      "from_user": "customer_user",
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
  "auth_user": "optional_auth_user",
  "contact_user": "optional_contact_user",
  "from_user": "optional_from_user",
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
- `password`

Optional fields:

- `display_name`, default `""`
- `auth_user`, default `user`; rendered as the PJSIP registration
  authentication username.
- `contact_user`, default `user`; rendered as the PJSIP registration Contact
  user.
- `from_user`, default `user`; rendered as the PJSIP endpoint From user.
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
    "auth_user": "optional_auth_user",
    "contact_user": "optional_contact_user",
    "from_user": "optional_from_user",
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

- `400`: invalid trunk ID, unsafe characters, invalid codec list, invalid
  interval, or empty password for an enabled trunk.
- `422`: missing required request field or malformed request body.
- `503`: SIP trunk registry is not configured.

### POST `/sip-trunks/{trunk_id}/connect`

Enables an existing trunk, regenerates the Asterisk include file, reloads PJSIP,
and sends a register command.

Response:

```json
{
  "trunk": {
    "trunk_id": "customer-1",
    "host": "sip.example.com",
    "user": "customer_user",
    "auth_user": "customer_user",
    "contact_user": "customer_user",
    "from_user": "customer_user",
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
    "host": "sip.example.com",
    "user": "customer_user",
    "auth_user": "customer_user",
    "contact_user": "customer_user",
    "from_user": "customer_user",
    "display_name": "Customer 1",
    "enabled": false,
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
    "host": "sip.example.com",
    "user": "customer_user",
    "auth_user": "customer_user",
    "contact_user": "customer_user",
    "from_user": "customer_user",
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
        "text": "Text to synthesize and play.",
        "response_to_event_id": "Optional event ID this answers.",
        "response_kind": "Optional structured response kind for runtime playback policy."
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
| `delegate_to_subagent` | Delegate complex work to a registered colleague/subagent provider. FlowHunt target IDs are integration configuration and cannot be supplied by the communication agent. |
| `invoke_flowhunt_flow` | Invoke the configured FlowHunt flow for complex work. The voice-agent tool schema accepts `call_id`, `message`, `response_to_event_id`, and `suppress_progress`; the runtime uses `VOICEBOT_FLOWHUNT_FLOW_ID` so the agent cannot invent a flow ID during a call. |
| `create_flowhunt_project_issue` | Create a FlowHunt AI Project issue for complex work. The project ID is integration configuration and cannot be supplied by the communication agent. |
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

The `say` tool accepts optional `response_kind` metadata. Runtime-generated
call-control acknowledgements use `call_control_ack` so the media layer can
play the acknowledgement before hangup, transfer, or DTMF without relying on
hardcoded caller-language keywords.

Tool argument details are available from `GET /agent/tools/schema`.

The colleague tools accept optional `suppress_progress`. When true, the tool
schedules the delegated work without speaking its default progress phrase. The
communication-agent worker uses this after it has already spoken a delayed
acknowledgement, which avoids duplicate "I am checking" messages while keeping
the tool safe for other agents that still need the built-in progress speech.

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
latency/failure rollups, latency breakdowns, and SLO checks.

Query parameters:

- `after`: optional event ID cursor.
- `call_id`: optional call filter.
- `workspace_id`: optional workspace filter.
- `voicebot_id`: optional voicebot filter.
- `session_id`: optional session filter.
- `limit`: maximum events, default `1000`.

### POST `/observability/evaluate`

Runs deterministic conversation checks against selected events.

### GET `/observability/slo`

Evaluates operational SLOs for the selected event slice. Optional filters match
`/observability/timeline`: `call_id`, `workspace_id`, `voicebot_id`,
`session_id`, and `limit`.

SLO checks include call-to-greeting audio, speech-to-transcript,
end-of-speech-to-first-audio, call setup rate, and provider error rate.

### GET `/observability/diagnostics`

Returns support-safe diagnostics for the selected event slice. The response
contains trace field names, timeline health, SLO state, provider failure counts,
slowest turn, category counts, and troubleshooting hints. Routine diagnostics do
not include transcript text or secrets.

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

### GET `/sip/media-plane`

Returns the SIP/Asterisk media-plane HA contract: local Docker behavior,
Kubernetes target architecture, readiness dimensions, trunk routing scope,
draining model, call-control routing, and the active-call failover boundary.

### GET `/webrtc/media-plane`

Returns the WebRTC scale contract: local browser test behavior, Kubernetes
signaling target, workspace/voicebot/channel routing, TURN/STUN requirements,
reconnect semantics, admission control, and quality metrics.

### GET `/realtime/audio-profile`

Returns the active realtime audio quality profile and validation issues. The
profile covers turn detection, barge-in cancellation, stale response dropping,
streaming TTS/STT contract support, jitter buffers, TTS cache, and regression
coverage.

### GET `/scaling/topology`

Returns worker roles, queue names, concurrency, shared state, and event bus
settings for the voicebot runtime.

### GET `/health/liveness`

Returns a lightweight liveness signal. Normal provider slowness or runtime
draining does not fail liveness.

### GET `/operations/drain`

Returns current drain state plus the rollout/failover contract.

### POST `/operations/drain/start`

Marks the runtime draining so readiness fails and new sessions should not be
accepted.

```json
{
  "reason": "rollout",
  "interrupt_active_sessions": false
}
```

Set `interrupt_active_sessions=true` in local Docker tests to stop active
sessions and emit `session_interrupted`.

### POST `/operations/drain/stop`

Clears runtime drain state and allows readiness to pass again.

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
  "agent_provider": "anthropic",
  "baseline_sessions": 40,
  "call_growth_per_minute": 30,
  "worker_warmup_seconds": 20,
  "max_concurrent_sessions": 100,
  "burst_sessions": 20,
  "scale_to_zero_allowed": false
}
```

The response includes queue capacity flags plus a `warm_capacity` block with
projected peak sessions and hard/burst cap checks.

### GET `/scaling/signals`

Returns autoscaling signals for active sessions, queue depth, warm-capacity
deficits, provider failures, latency metrics, calls per second, and worker
capacity. Optional query filters: `workspace_id`, `voicebot_id`.

Use `GET /scaling/signals?format=prometheus` for Prometheus text format.

### POST `/scaling/admission`

Evaluates whether a new call/session should be accepted before allocating
expensive media and provider resources.

```json
{
  "workspace_id": "workspace-1",
  "voicebot_id": "voicebot-1",
  "max_concurrent_sessions": 100,
  "burst_sessions": 20,
  "scale_to_zero_allowed": false
}
```

The response returns `decision=accept`, `queue_or_overflow`, or `reject`.

### POST `/routing/admission`

Resolves an incoming SIP/WebRTC channel to a workspace voicebot and performs
the full routed admission preflight before expensive media/provider resources
are allocated.

The runtime checks:

- channel or trunk/widget route
- workspace access
- voicebot enabled state
- provider or runtime config availability
- workspace/voicebot capacity
- optional session lease acquisition

Request:

```json
{
  "channel_kind": "webrtc_widget",
  "external_id": "widget-1",
  "session_id": "session-1",
  "owner": "voicebot-pod-1",
  "transport": "webrtc",
  "call_id": "call-1",
  "acquire_lease": true,
  "lease_ttl_seconds": 30,
  "max_concurrent_sessions": 100,
  "burst_sessions": 20
}
```

Accepted sessions return `allowed=true`, route scope, capacity details, and the
lease when acquired. Rejected sessions return a deterministic reason and a
transport-specific fallback description. SIP fallbacks include busy/unavailable
or transfer options; WebRTC fallbacks use a structured HTTP error before SDP
answer.

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

Lists active workers. Optional query filters: `role`, `workspace_id`,
`voicebot_id`.

### GET `/scaling/capacity`

Summarizes active worker capacity by role. Optional query filter:
`workspace_id`, `voicebot_id`.

### GET `/scaling/session-leases`

Lists active session ownership leases. Optional query filters:
`workspace_id`, `voicebot_id`.

### POST `/scaling/session-leases/acquire`

Acquires ownership for one active session.

```json
{
  "workspace_id": "workspace-1",
  "voicebot_id": "voicebot-1",
  "session_id": "session-1",
  "owner": "voicebot-pod-1",
  "ttl_seconds": 30,
  "call_id": "call-1",
  "transport": "webrtc",
  "metadata": {"node": "local-docker"}
}
```

### POST `/scaling/session-leases/renew`

Renews ownership for a session when the current owner still holds the lease.
The request shape matches acquire.

### POST `/scaling/session-leases/release`

Releases ownership on clean call end.

```json
{
  "workspace_id": "workspace-1",
  "voicebot_id": "voicebot-1",
  "session_id": "session-1",
  "owner": "voicebot-pod-1"
}
```

### POST `/scaling/session-leases/expire`

Expires abandoned leases and emits `session_lease_expired` events. This is a
local operational endpoint for Docker testing; production should run equivalent
expiration through the shared lease backend or a coordinator.

### POST `/scaling/session-leases/enforce`

Stops active media sessions that no longer have a valid owner lease and emits
`session_lease_lost`, `session_interrupted`, and `session_recovered` events.

```json
{
  "owner": "voicebot-pod-1",
  "stop_unleased_sessions": true,
  "recover_non_media_work": true
}
```

### GET `/scaling/queue`

Returns local worker queue pending and claimed snapshots.

### GET `/scaling/queue/priorities`

Returns the configured priority classes and routing rules. Claim order is
`high`, then `normal`, then `background`, with FIFO preserved inside each
priority.

### POST `/scaling/queue/enqueue`

Enqueues a worker item in the local queue lifecycle store.

```json
{
  "item_id": "item-1",
  "kind": "agent_turn",
  "routing": {"workspace_id": "workspace-1", "voicebot_id": "voicebot-1", "session_id": "session-1"},
  "queue": "voicebot.agent",
  "payload": {"event_id": 42},
  "trace_id": "trace-1",
  "priority": "normal",
  "idempotency_key": "session-1:event-42",
  "max_attempts": 3
}
```

### POST `/scaling/queue/claim`

Claims pending worker items by queue.

```json
{
  "queue": "voicebot.agent",
  "owner": "agent-worker-1",
  "limit": 1,
  "ttl_seconds": 30
}
```

### POST `/scaling/queue/renew`

Renews the claim TTL for one claimed worker item.

```json
{"item_id": "item-1", "owner": "agent-worker-1", "ttl_seconds": 30}
```

### POST `/scaling/queue/ack`

Acknowledges a claimed worker item after successful processing.

```json
{"item_id": "item-1", "owner": "agent-worker-1"}
```

### POST `/scaling/queue/release`

Releases a claimed worker item back to pending. If the item has reached
`max_attempts`, it moves to the dead-letter set instead of pending.

```json
{"item_id": "item-1", "owner": "agent-worker-1", "error": "provider timeout"}
```

### GET `/scaling/queue/dead-letter`

Returns terminal failed worker items with their last error and failed timestamp.

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
