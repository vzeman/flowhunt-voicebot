# Production Runbook

This runbook is the operator entry point for deploying and operating the
FlowHunt voicebot runtime after the API split, public route model, dashboard,
widget, auth, storage, and Kubernetes topology contracts.

## API Surfaces

| Surface | Example endpoints | Exposure | Auth |
| --- | --- | --- | --- |
| Public caller API | `GET /.well-known/flowhunt-voicebot`, `GET /widget.js`, `GET /widget`, `POST /webrtc/sessions`, `DELETE /webrtc/sessions/{session_id}`, `GET /openapi/public.json`, health liveness | Internet public ingress | Anonymous, protected by route resolution, origin allow-list, rate limits, and admission checks |
| Internal API | `/workspaces/...`, `/dashboard/state`, `/events`, `/calls`, `/subagent`, `/scaling`, `/observability`, `/storage`, `/security`, `/config`, `/openapi/internal.json` | Private cluster/VPN/service mesh | Internal API key or future workload identity |
| Dashboard | `GET /dashboard`, `/webrtc/test` behind private dashboard ingress | Private dashboard ingress | Dashboard user login/SSO/RBAC; local dev bypass only when explicitly enabled |
| Local dev only | `/webrtc/test` when using local Docker | Local machine/private dashboard only | Internal/local dashboard access |

Generate or export OpenAPI specs:

```bash
curl http://127.0.0.1:8080/openapi/public.json > public-openapi.json
curl -H "X-FlowHunt-Internal-Key: $VOICEBOT_INTERNAL_KEY" \
  http://127.0.0.1:8080/openapi/internal.json > internal-openapi.json
```

Public OpenAPI must not contain dashboard, event, task queue, diagnostics,
config, transcript, recording, call-control, or internal admin endpoints.
`GET /api/surface` reports route-audience classification and should have no
unclassified routes before deployment.

## Traffic Diagrams

Public browser/widget traffic:

```text
Customer website
  |
  | loads https://voice.example.com/widget.js
  v
public HTTPS ingress
  |
  | preserves Host / X-Forwarded-Host / forwarded path
  v
voicebot-public-api
  |
  | resolves PublicVoicebotRoute and creates WebRTC session
  v
voicebot-webrtc-media + managed STUN/TURN
```

Internal operations traffic:

```text
FlowHunt backend / workers / private dashboard
  |
  | internal API key or user SSO/RBAC
  v
private ingress / ClusterIP
  |
  +--> voicebot-internal-api
  +--> voicebot-dashboard
  +--> voicebot-agent-workers / task pollers
```

SIP traffic:

```text
SIP provider
  |
  | SIP 5060 UDP + RTP UDP range
  v
voicebot-sip-media / Asterisk edge
  |
  | AudioSocket TCP 9019
  v
voicebot media/session runtime
```

## Custom Public URL Routing

Use public route records to map custom hosts and path prefixes to a workspace
voicebot channel:

```json
{
  "route_id": "support-public",
  "channel_id": "support-widget",
  "host": "voice.example.com",
  "path_prefix": "/support",
  "status": "active",
  "tls_mode": "managed",
  "allowed_origins": ["https://www.example.com"]
}
```

Operational rules:

- Public ingress must forward the original host and path through
  `Host`/`X-Forwarded-Host` plus `X-Forwarded-Prefix`, `X-Original-URI`, or
  `X-Forwarded-URI`.
- Active `host + path_prefix` pairs are globally unique. Conflicts are rejected
  even across workspaces.
- `tls_mode=managed` means the ingress/certificate layer owns provisioning.
  `tls_mode=custom` means certificate ownership must be handled outside the
  runtime before traffic is routed.
- `allowed_origins` controls browser CORS and session creation. Leave it empty
  only for intentional unrestricted test routes.
- Any route/widget token is public identity, not a secret.

## Dashboard Operations

Dashboard URL:

```text
https://internal-voicebot.example.com/dashboard
```

Operators can currently inspect:

- workspaces and voicebots
- enabled/disabled state
- channel and public route counts
- active WebRTC sessions
- recent workspace events
- WebRTC inference console via `/webrtc/test`

Management APIs already exist for voicebots, channels, public routes, prompts,
providers, runtime config, SIP trunks, and retention hooks. The dashboard must
call only internal APIs, and all mutations must remain workspace/voicebot
scoped and audited. Raw provider keys, SIP passwords, and other secrets must
never be displayed after creation.

Dashboard auth:

- Production should provide FlowHunt user identity/SSO/RBAC at the private
  dashboard ingress or backend gateway.
- `VOICEBOT_DASHBOARD_AUTH_ENABLED=true` requires user identity headers.
- Service API keys are not dashboard user login.
- `VOICEBOT_DASHBOARD_DEV_LOGIN_ENABLED=true` works only in local/dev/test and
  only with `X-FlowHunt-Dev-Login: true`.

## Kubernetes Topology

Target services:

| Service | Exposure | Purpose |
| --- | --- | --- |
| `voicebot-public-api` | Internet HTTPS | Public bootstrap, widget, public OpenAPI, WebRTC signaling |
| `voicebot-internal-api` | Private | FlowHunt backend, workers, admin, operations APIs |
| `voicebot-dashboard` | Private | Dashboard UI and private WebRTC test console |
| `voicebot-webrtc-media` | Public media edge | WebRTC sessions and ICE/TURN media |
| `voicebot-sip-media` | SIP provider edge | SIP signaling, RTP, Asterisk AudioSocket |
| `voicebot-agent-workers` | Private | STT/TTS/agent/session/post-call workers |
| `voicebot-task-pollers` | Private | Subagent lifecycle polling |

Probe model:

- Liveness: `GET /health/liveness`
- Aggregate readiness: `GET /health/readiness`
- Role routing readiness: `GET /health/readiness/roles`
- Topology contract: `GET /deployment/topology`

Network policy expectations:

- Public ingress forwards only public-audience endpoints.
- Internal APIs require internal auth and are reachable only from FlowHunt
  backend services and runtime workers.
- Dashboard ingress is private and requires dashboard user auth.
- SIP/RTP ports are exposed only through provider peering or a dedicated UDP
  load balancer, never through public web ingress.
- Redis/queue/database/object storage access is limited to runtime services.

## Local Development Parity

Default Docker Compose runs a combined `voicebot` service plus Asterisk and
agent workers. This maps to production split services through
`VOICEBOT_RUNTIME_ROLES=all`.

Useful local commands:

```bash
docker compose up -d --build
curl http://127.0.0.1:8080/health/readiness
curl http://127.0.0.1:8080/deployment/topology
```

Enable internal auth locally:

```text
VOICEBOT_INTERNAL_AUTH_ENABLED=true
VOICEBOT_INTERNAL_API_KEYS=admin:control-plane:secret-value:internal:*
```

Enable dashboard user auth locally with explicit dev login:

```text
VOICEBOT_DASHBOARD_AUTH_ENABLED=true
VOICEBOT_DASHBOARD_DEV_LOGIN_ENABLED=true
```

Run production-like backing services:

```bash
docker compose --profile production-like up -d postgres redis
```

## Onboard A Workspace Voicebot

1. Create the workspace voicebot with display name and enabled state.
2. Configure provider references for STT, TTS, communication agent, and
   subagents. Store only secret references, not raw keys in API responses.
3. Configure prompts and language policy.
4. Configure realtime behavior: VAD, silence, barge-in, chunking, and latency
   budgets.
5. Create a channel: WebRTC widget, SIP trunk, or future transport.
6. Create a public route if the channel is public WebRTC.
7. Set `allowed_origins`, rate limits, max concurrent sessions, and TLS mode.
8. Validate the voicebot: `POST /workspaces/{workspace_id}/voicebots/{voicebot_id}/validate`.
9. Test from the internal dashboard WebRTC console.
10. Test the public widget from an allowed website origin.
11. Confirm `session_admission_decided`, `api_access_logged`, and
    `security_audit` events appear as expected.

## Debug A Live Call

1. Check `GET /health/readiness/roles` for the relevant service role.
2. Open `/dashboard` and select the workspace.
3. Check active sessions and the event stream.
4. Inspect `api_access_logged` for public/internal request status and latency.
5. Inspect `session_admission_decided` for route, origin, rate-limit, or
   capacity decisions.
6. Inspect `user_transcript`, `stt_*`, `agent_*`, `tts_*`, and playback events
   for turn latency.
7. Inspect subagent task state if the communication agent delegated work.
8. Use `/observability/slo` and `/observability/diagnostics` for support-safe
   latency and failure hints.
9. After call end, review transcript and speech-only recording if retention
   policy allows.
10. For data deletion, call
    `POST /workspaces/{workspace_id}/security/retention/delete` first with
    `dry_run=true`, then submit the real request with `dry_run=false` if the
    hooks and scope are correct.
