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
    Runtime config
      STT/TTS/agent providers
      FlowHunt flow/project bindings
      prompts, language, voice
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
workspace or voicebot.

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
binding before registering a different route.

## Subagents

Subagent and FlowHunt project/flow calls must run in the same `workspace_id` as
the voicebot session. `require_same_workspace()` makes this invariant explicit.

## Current Prototype Assumptions To Remove

- Global `.env` provider choices instead of workspace/voicebot provider config
- Local in-memory channel routing in some runtime paths
- Local worker lease state for agent tasks
- Local JSON stores for events and external task records
- Prototype browser test endpoint outside workspace routing

## Follow-Up Implementation Work

- Persist `voicebots` and `voicebot_channels` in FlowHunt DB
- Route SIP/WebRTC runtime sessions through channel bindings
- Store active sessions, events, transcripts, and external tasks with
  `workspace_id`, `voicebot_id`, and `session_id`
- Enforce FlowHunt workspace permissions on admin APIs
- Move task leases and active session locks to shared storage
