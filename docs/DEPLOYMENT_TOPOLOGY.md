# Deployment Topology And Local-To-Cloud Parity

The local Docker setup still runs as a compact developer environment, but the
runtime now exposes an explicit deployment-role contract so the same code can be
split into Kubernetes deployments later without changing the voicebot core.

## Runtime Role Selection

`VOICEBOT_RUNTIME_ROLES` controls which roles a process is intended to serve.
The default is:

```text
VOICEBOT_RUNTIME_ROLES=all
```

`all` keeps the current local behavior: one `voicebot` service exposes the API,
AudioSocket, WebRTC sessions, subagent polling, state stores, and coordination
surfaces, while Docker Compose also starts Asterisk and a communication-agent
container.

For split-role testing, run additional voicebot containers with a comma-separated
subset:

```text
VOICEBOT_RUNTIME_ROLES=api_control_plane,subagent_task_poller
```

The current implementation exposes role intent and role-specific readiness. It
does not yet disable every code path per role; that final separation should
happen when queue-backed workers replace in-process execution.

## Role Catalog

`GET /deployment/topology` returns the role catalog, enabled roles, unknown role
names, local process mapping, future Kubernetes deployment names, queue names,
target services, ingress boundaries, port matrix, readiness checks, startup
probe suggestions, and resource profiles.

## Service Diagram

```text
Internet browser/widget
        |
        v
 public HTTPS ingress + custom host/path routing
        |
        v
 voicebot-public-api  --->  voicebot-webrtc-media  --->  managed STUN/TURN
        |
        v
 shared stores / queues / session state
        ^
        |
 voicebot-agent-workers <--- voicebot-task-pollers ---> subagent providers
        ^
        |
 voicebot-internal-api <--- FlowHunt backend / workers / admin services
        ^
        |
 voicebot-dashboard <--- private dashboard ingress + login/SSO

SIP provider
        |
        v
 SIP/RTP edge load balancer
        |
        v
 voicebot-sip-media
```

Local Docker still runs most of these responsibilities inside one `voicebot`
container, plus the `asterisk` container for SIP media. Kubernetes should split
the responsibilities by service boundary before exposing production traffic.

Current roles:

| Role | Local process | Future deployment | Target service(s) | Queue |
| --- | --- | --- | --- | --- |
| `api_control_plane` | `voicebot` | `voicebot-internal-api` | `voicebot-public-api`, `voicebot-internal-api`, `voicebot-dashboard` | none |
| `sip_media_ingress` | `asterisk + voicebot audiosocket` | `voicebot-sip-media` | `voicebot-sip-media` | `voicebot.media` |
| `webrtc_media_session` | `voicebot webrtc manager` | `voicebot-webrtc-media` | `voicebot-webrtc-media` | `voicebot.media` |
| `session_orchestrator` | `voicebot` | `voicebot-session-orchestrator` | `voicebot-agent-workers` | `voicebot.session` |
| `stt_worker` | `voicebot` | `voicebot-stt-worker` | `voicebot-agent-workers` | `voicebot.stt` |
| `tts_worker` | `voicebot` | `voicebot-tts-worker` | `voicebot-agent-workers` | `voicebot.tts` |
| `communication_agent_worker` | `openai-agent` or `anthropic-agent` | `voicebot-agent-workers` | `voicebot-agent-workers` | `voicebot.agent` |
| `subagent_task_poller` | `voicebot lifespan task poller` | `voicebot-task-pollers` | `voicebot-task-pollers` | `voicebot.external_tasks` |
| `post_call_worker` | `voicebot` | `voicebot-post-call-worker` | `voicebot-agent-workers` | `voicebot.post_call` |

## Target Services

| Service | Exposure | Ingress | Authentication | OpenAPI |
| --- | --- | --- | --- | --- |
| `voicebot-public-api` | Internet HTTP/WebRTC signaling | Public HTTPS ingress with managed/custom TLS | None for caller connection endpoints; public route admission enforces route, origin, rate, and capacity | `/openapi/public.json` |
| `voicebot-internal-api` | Private cluster or VPN | Private ingress or ClusterIP | Internal API key now, workload identity later | `/openapi/internal.json` |
| `voicebot-dashboard` | Private network | Private dashboard ingress | Internal auth now, FlowHunt SSO/RBAC later | none |
| `voicebot-webrtc-media` | Internet media edge | Public signaling plus managed STUN/TURN | Public route admission before session allocation | none |
| `voicebot-sip-media` | SIP provider edge | UDP load balancer or provider peering | SIP trunk credentials plus workspace channel routing | none |
| `voicebot-agent-workers` | Private cluster | none | Internal API key or workload identity | none |
| `voicebot-task-pollers` | Private cluster | none | Internal API key or workload identity | none |

## Port Matrix

| Service | Port | Protocol | Purpose |
| --- | --- | --- | --- |
| `voicebot-public-api` | 8080 | TCP | Public bootstrap, public OpenAPI, WebRTC offer/answer |
| `voicebot-internal-api` | 8080 | TCP | FlowHunt backend, worker, admin, and operations APIs |
| `voicebot-dashboard` | 8080 | TCP | Private dashboard and local WebRTC test UI |
| `voicebot-webrtc-media` | 8080 | TCP | SDP signaling |
| `voicebot-webrtc-media` | ephemeral | UDP | WebRTC ICE media candidates; production should prefer TURN |
| `voicebot-sip-media` | 5060 | UDP | SIP signaling |
| `voicebot-sip-media` | 10000-10100 | UDP | RTP media |
| `voicebot-sip-media` | 9019 | TCP | Asterisk AudioSocket bridge to voicebot |
| `voicebot-agent-workers` | 8080 | TCP | Worker health and internal task APIs |
| `voicebot-task-pollers` | 8080 | TCP | Poller health and task lifecycle APIs |

## Ingress And Security Boundaries

| Boundary | Exposure | Allowed audience | Must not expose |
| --- | --- | --- | --- |
| `public-web` | Internet | Public route-audience endpoints only | Internal OpenAPI, dashboard, events, task queues, diagnostics, config |
| `internal-private` | Private cluster or VPN | Internal endpoints | Anonymous browser traffic |
| `dashboard-private` | Private network | Dashboard/local-dev plus required internal APIs | Anonymous internet access |
| `sip-provider-edge` | SIP provider network | SIP and RTP media | HTTP APIs, dashboard, OpenAPI |

Public ingress must preserve `Host`/`X-Forwarded-Host` and forwarded path
headers so the runtime can resolve `PublicVoicebotRoute` records. It must route
only `/.well-known/flowhunt-voicebot`, `/openapi/public.json`, health liveness,
and public WebRTC session creation. Internal OpenAPI, dashboard pages, event
streams, task queues, diagnostics, configuration, transcripts, and call-control
APIs stay on private services with internal authentication.

Internal ingress must require `VOICEBOT_INTERNAL_AUTH_ENABLED=true` in
production and provide the configured internal API key header or future
workload identity. Dashboard ingress must remain private and later move behind
FlowHunt login, SSO, and RBAC.

SIP/RTP exposure is not HTTP ingress. It should use a UDP load balancer,
provider peering, or a dedicated media edge. SIP signaling, RTP ranges, AMI,
and AudioSocket ports must not be exposed through the public web ingress.

## WebRTC ICE/STUN/TURN

Local Docker can use public STUN for browser tests. Production browser traffic
must use a managed STUN/TURN strategy. Public bootstrap exposes `ice_servers`
to the widget, but Kubernetes pods must not rely on pod IP candidates being
reachable from browsers. TURN credentials should be short lived and scoped to
the resolved voicebot route/session.

Queue workers must honor the runtime priority contract. High-priority work
includes barge-in, stop playback, hangup, transfer, and DTMF. Normal work covers
ordinary STT, agent, and TTS turns. Background work covers subagent polling,
summaries, post-call jobs, analytics, compaction, and slow retries. Kubernetes
can implement this with one queue carrying a priority field or with separate
high/normal/background streams, but high-priority work must overtake background
work before a worker claim is granted.

## Role Readiness

`GET /health/readiness/roles` derives role-specific readiness from the existing
readiness checks. Examples:

- API control plane checks providers, event catalog, security contract, durable
  storage, and drain state.
- SIP media checks AMI config, SIP media-plane contract, realtime audio profile,
  and drain state.
- WebRTC media checks WebRTC media-plane contract, realtime audio profile, and
  drain state.
- Subagent pollers check storage, durable state, and security contract.

`GET /health/readiness` remains the aggregate readiness endpoint. Liveness stays
at `GET /health/liveness`.

The role readiness payload also includes a `routing` section:

- `public_http_webrtc`: safe only when API control plane and WebRTC media roles
  are enabled and ready.
- `internal_api`: safe when the API control plane role is ready.
- `sip_media`: safe when SIP media ingress is ready.
- `worker_queues`: safe when orchestration, STT, TTS, communication agent, and
  subagent poller roles are ready.

Ingress controllers, service meshes, and rollout automation should use this
role-specific readiness instead of the aggregate endpoint when routing to split
Kubernetes services.

## Docker Parity

Docker Compose remains optimized for local development:

- `voicebot`: all-in-one runtime by default
- `asterisk`: local SIP/PJSIP and RTP media entrypoint
- `openai-agent`: communication-agent worker
- `anthropic-agent`: optional communication-agent worker through the
  `anthropic` profile

To test future split behavior locally, run multiple `voicebot` containers with
different `VOICEBOT_RUNTIME_ROLES` values and point them at shared JSON paths or
future shared stores. The current JSON stores are still local scaffolding; full
production split requires Redis/queue/database-backed stores.

## Kubernetes Preparation

This issue intentionally does not add manifests. The exposed contract prepares:

- per-role Deployments
- role-specific readiness, liveness, and startup probes
- PodDisruptionBudgets for media and worker roles
- HPA inputs from `/scaling/signals`
- workspace-scoped secret injection
- safe database and queue migration hooks
- release/rollback through drain state and worker presence

Actual Kubernetes YAML/Helm work should consume this role catalog instead of
redefining roles independently.
