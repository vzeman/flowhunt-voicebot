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
internal key header.

Dashboard user authentication is configured separately from internal service
API keys:

```text
VOICEBOT_DASHBOARD_AUTH_ENABLED=true
VOICEBOT_DASHBOARD_USER_ID_HEADER=X-FlowHunt-User-Id
VOICEBOT_DASHBOARD_WORKSPACE_IDS_HEADER=X-FlowHunt-Workspace-Ids
```

When dashboard auth is enabled, a service API key alone is not a dashboard user
login. The request must include an authenticated user id and the workspace ids
that user may access. Hosted production should populate these headers from
FlowHunt user identity, SSO, and workspace permissions at the private dashboard
ingress or backend gateway.

Local development can enable an explicit bypass:

```text
VOICEBOT_DASHBOARD_AUTH_ENABLED=true
VOICEBOT_DASHBOARD_DEV_LOGIN_ENABLED=true
```

The bypass works only in local/development/test deployment modes and only when
the request includes `X-FlowHunt-Dev-Login: true`.

## Current Capabilities

- Workspace selector based on configured workspace-scoped voicebots.
- Voicebot cards with enabled state, channel count, public route count, and
  active WebRTC session count.
- Active WebRTC session table.
- Embedded WebRTC inference console. There is no standalone `/webrtc/test`
  route.
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

The current dashboard state endpoint already filters workspaces by the
authenticated user's workspace list and emits a `security_audit` event with the
dashboard user id for every authenticated dashboard request.

## Security Boundary

The dashboard must be routed only through the private dashboard ingress
described in `docs/DEPLOYMENT_TOPOLOGY.md`. Public voicebot ingress must never
expose `/dashboard`, `/dashboard/state`, internal OpenAPI, events,
transcripts, diagnostics, task queues, or call-control APIs.
