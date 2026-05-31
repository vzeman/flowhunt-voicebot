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

For local Docker testing, the service seeds one default voicebot when
`VOICEBOT_DEFAULT_WORKSPACE_ID` is set. If it is empty, the runtime falls back
to `FLOWHUNT_WORKSPACE_ID`. The default voicebot id and name are controlled by:

```text
VOICEBOT_DEFAULT_VOICEBOT_ID=default
VOICEBOT_DEFAULT_VOICEBOT_DISPLAY_NAME=Default Voicebot
```

This local seed gives the dashboard a workspace and voicebot target for the
embedded WebRTC test console. Production should create voicebots through the
workspace admin APIs or FlowHunt control plane instead of relying on this seed.

## Current Capabilities

- Main menu with `Workspaces`, `Active Sessions`, `Sessions History`, and
  `Voicebot Test`.
- Workspace table showing `workspace_id` and display name. Opening a workspace
  shows its voicebots.
- Voicebot detail view with editable basic settings and prompts, including the
  greeting, filler message, system prompt, STT prompt, and language, plus
  read-only provider and runtime configuration JSON.
- Active session table showing workspace, voicebot, session id, status, start
  time, and elapsed length.
- Finished session history table with the same operational columns.
- Session detail view with event timeline, transcript, and call recording
  playback when a recording artifact exists.
- Embedded WebRTC voicebot test console. The dashboard-level test selector
  chooses workspace and voicebot, and that target is passed into the WebRTC
  session metadata. There is no standalone `/webrtc/test` route.

## Management Model

The dashboard reads state from existing internal APIs and runtime stores.
Voicebot settings and prompt edits call the existing workspace-scoped admin
APIs:

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
