# Internal Dashboard

The dashboard is an internal operations and voicebot administration surface.
It is served at:

```text
GET /dashboard
GET /dashboard/state
```

Both routes are classified as internal API surface. In local Docker, they work
with the same permissive defaults as the rest of the internal API. When
`VOICEBOT_INTERNAL_AUTH_ENABLED=true`, callers must provide the configured
internal key header. Production user login, FlowHunt SSO, and RBAC are tracked
separately and must be enabled before exposing the dashboard outside a private
network.

## Current Capabilities

- Workspace selector based on configured workspace-scoped voicebots.
- Voicebot cards with enabled state, channel count, public route count, and
  active WebRTC session count.
- Active WebRTC session table.
- Embedded WebRTC inference console using the existing `/webrtc/test` tool.
- Recent workspace event JSON for debugging.

## Management Model

The dashboard reads state from existing internal APIs and runtime stores. Full
editing will continue to use the workspace-scoped admin APIs:

- `/workspaces/{workspace_id}/voicebots`
- `/workspaces/{workspace_id}/voicebots/{voicebot_id}/channels`
- `/workspaces/{workspace_id}/voicebots/{voicebot_id}/public-routes`
- `/workspaces/{workspace_id}/voicebots/{voicebot_id}/providers`
- `/workspaces/{workspace_id}/voicebots/{voicebot_id}/prompts`
- `/workspaces/{workspace_id}/voicebots/{voicebot_id}/runtime-config`
- SIP trunk management APIs

All future dashboard mutations must remain workspace/voicebot scoped, audited,
and protected by dashboard login/RBAC. Secrets must be shown only as references
or configured/not-configured metadata.

## Security Boundary

The dashboard must be routed only through the private dashboard ingress
described in `docs/DEPLOYMENT_TOPOLOGY.md`. Public voicebot ingress must never
expose `/dashboard`, `/dashboard/state`, `/webrtc/test`, internal OpenAPI,
events, transcripts, diagnostics, task queues, or call-control APIs.
