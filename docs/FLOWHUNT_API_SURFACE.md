# FlowHunt Voicebot API Surface

This document defines the product API shape for integrating voicebots into
FlowHunt workspaces. It separates admin/config APIs from runtime call/session
APIs and marks prototype-only endpoints.

## Principles

- Public APIs are workspace-scoped.
- Admin/config APIs are separate from runtime session APIs.
- Worker/internal APIs are not exposed as product APIs.
- Prototype endpoints must be explicitly marked for removal or internal-only
  use.

## Surface Discovery

`GET /api/surface` returns the grouped API surface catalog, whether all public
endpoints are workspace-scoped, and any scope violations. Each endpoint declares
`scope_source`:

- `path`: workspace is in `/workspaces/{workspace_id}/...`
- `payload`: workspace/voicebot route is resolved from request data
- `route_binding`: workspace is resolved from an existing channel/trunk binding
- `none`: prototype or internal endpoint without workspace permission scope

`GET /api/surface/prototypes` returns prototype-only endpoints that must not be
exposed as public product APIs.

## Admin APIs

- `GET /workspaces/{workspace_id}/voicebots`
- `POST /workspaces/{workspace_id}/voicebots`
- `GET /workspaces/{workspace_id}/voicebots/{voicebot_id}`
- `PATCH /workspaces/{workspace_id}/voicebots/{voicebot_id}`
- `DELETE /workspaces/{workspace_id}/voicebots/{voicebot_id}`
- `POST /workspaces/{workspace_id}/voicebots/{voicebot_id}/validate`

## Channel APIs

- `GET /workspaces/{workspace_id}/voicebots/{voicebot_id}/channels`
- `POST /workspaces/{workspace_id}/voicebots/{voicebot_id}/channels`
- `PATCH /workspaces/{workspace_id}/voicebots/{voicebot_id}/channels/{channel_id}`
- `DELETE /workspaces/{workspace_id}/voicebots/{voicebot_id}/channels/{channel_id}`

Channel types include SIP trunk bindings and WebRTC widget/token bindings.

## Provider APIs

- `GET /workspaces/{workspace_id}/voicebots/{voicebot_id}/providers`
- `PUT /workspaces/{workspace_id}/voicebots/{voicebot_id}/providers`

Provider config uses secret references and must validate before channel enable.

## Runtime APIs

- `POST /runtime/webrtc/sessions`
- `POST /runtime/sip-trunks/{trunk_id}/register`

Runtime APIs create or update active call/session bindings. They should resolve
workspace and voicebot from channel/trunk/widget configuration.

## Session APIs

- `GET /workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions`
- `GET /workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}`
- `GET /workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}/timeline`
- `GET /workspaces/{workspace_id}/voicebots/{voicebot_id}/sessions/{session_id}/transcript`

## Task APIs

- `GET /workspaces/{workspace_id}/voicebots/{voicebot_id}/tasks`

This returns delegated subagent/external task status scoped to the workspace and
voicebot.

## Internal And Prototype APIs

- `/agent/tasks` is an internal worker lease API.
- `/webrtc/test` is a prototype local browser test app.

These should not be exposed as public product endpoints.
