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
readiness checks, startup probe suggestions, and resource profiles.

Current roles:

| Role | Local process | Future deployment | Queue |
| --- | --- | --- | --- |
| `api_control_plane` | `voicebot` | `voicebot-api` | none |
| `sip_media_ingress` | `asterisk + voicebot audiosocket` | `voicebot-sip-media` | `voicebot.media` |
| `webrtc_media_session` | `voicebot webrtc manager` | `voicebot-webrtc-media` | `voicebot.media` |
| `session_orchestrator` | `voicebot` | `voicebot-session-orchestrator` | `voicebot.session` |
| `stt_worker` | `voicebot` | `voicebot-stt-worker` | `voicebot.stt` |
| `tts_worker` | `voicebot` | `voicebot-tts-worker` | `voicebot.tts` |
| `communication_agent_worker` | `openai-agent` or `anthropic-agent` | `voicebot-agent-worker` | `voicebot.agent` |
| `subagent_task_poller` | `voicebot lifespan task poller` | `voicebot-subagent-poller` | `voicebot.external_tasks` |
| `post_call_worker` | `voicebot` | `voicebot-post-call-worker` | `voicebot.post_call` |

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
