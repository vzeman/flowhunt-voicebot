# Workspace-Based Multitenancy

FlowHunt workspaces are the tenant boundary. The voicebot runtime must not
introduce a separate `tenant_id` model.

## Hierarchy

```text
Workspace
  Voicebot
    Channels
      SIP trunks
      phone numbers
      WebRTC widgets
    Public routes
      custom hosts
      managed subdomains
      path prefixes
    Runtime config
      STT/TTS/agent providers
      FlowHunt flow/project bindings
      prompts, language, voice
      config_version
    Sessions
      customer calls and browser sessions
    Events, transcripts, external tasks
```

## Required Runtime Scope

Runtime paths should carry:

- `workspace_id`
- `voicebot_id`
- `session_id`

`WorkspaceScope` models these identifiers. Events should include this scope when
the channel or session route is known.

`VoicebotSessionRecord` is the normalized per-call/per-browser-session entity.
It carries `workspace_id`, `voicebot_id`, `session_id`, optional channel and
external session ids, status, timestamps, and metadata. `VoicebotSessionStore`
is the first in-memory contract for listing active/concurrent sessions by
workspace and voicebot; production should back the same shape with FlowHunt DB
or another shared store. A saved session id cannot be reassigned to a different
workspace or voicebot. Session records reject unsupported statuses,
timezone-less timestamps, and ended sessions without `ended_at`.

## Channel Resolution

Inbound traffic resolves through channel bindings:

```text
SIP trunk / phone number / WebRTC widget
        |
        v
VoicebotChannelBinding
        |
        v
workspace_id + voicebot_id
        |
        v
create session_id
```

The first implementation provides an in-memory `ChannelResolver` contract. In
FlowHunt production, channel bindings should live in workspace-scoped database
tables. The resolver supports registering and unregistering bindings by route
key or channel id so the admin layer can connect/disconnect SIP trunks, phone
numbers, and WebRTC widgets without restarting runtime workers.

Channel bindings are identity-guarded. A route cannot be reassigned to another
channel id, and an existing channel id cannot silently move across route,
workspace, or voicebot. Dynamic disconnect/reconnect should unregister the old
binding before registering a different route. Bindings also reject blank channel,
workspace, voicebot, and external route ids, plus unsupported channel kinds,
before they enter the resolver.

## Public URL Routing

`PublicVoicebotRoute` maps an external public URL to a workspace voicebot
channel:

```text
Host + path prefix
        |
        v
PublicVoicebotRoute
        |
        v
workspace_id + voicebot_id + channel_id
        |
        v
WebRTC session metadata and lifecycle events
```

Routes are workspace and voicebot scoped. One active `host + path_prefix`
combination can only point to one route across the runtime, which keeps
production ingress routing unambiguous. Disabled or pending routes can coexist
with an active route but do not resolve public sessions.

The first runtime resolver uses `Host`/`X-Forwarded-Host` and forwarded path
headers such as `X-Forwarded-Prefix`, `X-Original-URI`, or `X-Forwarded-URI`.
This lets an ingress rewrite public custom URLs to the internal
`POST /webrtc/sessions` endpoint while preserving enough information for the
voicebot to resolve the correct workspace, voicebot, and channel.

## Subagents

Subagent and FlowHunt project/flow calls must run in the same `workspace_id` as
the voicebot session. `require_same_workspace()` makes this invariant explicit.

## Security And Audit

`GET /security/contract` exposes the current workspace isolation contract.
Local Docker mode is permissive by default, while production mode must enable
workspace authorization. The readiness report includes `security_contract` so
misconfigured production enforcement is visible before accepting sessions.

Security-sensitive actions emit `security_audit` events with recursively
redacted metadata. The current audit surface covers call control, provider and
runtime config changes, workspace transcript reads, SIP trunk changes, and
explicit workspace audit submissions.

Retention classes for events, transcripts, recordings, cached TTS audio, and
subagent tasks are returned by
`GET /workspaces/{workspace_id}/security/retention`.

## Current Prototype Assumptions To Remove

- Global `.env` provider choices instead of workspace/voicebot provider config
- Local in-memory channel routing in some runtime paths
- Local worker lease state for agent tasks
- Local JSON stores for events and external task records
- Prototype browser test endpoint outside workspace routing
- Legacy local SIP trunk commands without workspace route binding

## Runtime Config Versioning

`VoicebotRuntimeConfig` is the control-plane object for runtime behavior inside
one workspace and voicebot. It bundles provider selections, secret references,
prompts, language, realtime audio tuning, quotas, enabled actions, and subagent
bindings under a monotonically increasing `config_version`.

Saving a runtime config emits `runtime_config_updated`. New sessions should use
the latest enabled version, while active sessions keep the version they started
with for auditability and predictable call behavior.

## Follow-Up Implementation Work

- Persist `voicebots` and `voicebot_channels` in FlowHunt DB
- Route SIP/WebRTC runtime sessions through channel bindings
- Store active sessions, events, transcripts, and external tasks with
  `workspace_id`, `voicebot_id`, and `session_id`
- Replace the local workspace allow-list with FlowHunt's production permission
  service
- Move task leases and active session locks to shared storage
