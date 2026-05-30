# Failover And Draining

This runtime distinguishes liveness from readiness:

- `GET /health/liveness` stays lightweight and should only fail for
  unrecoverable stuck runtime states.
- `GET /health/readiness` fails while the runtime is draining because it should
  not accept new sessions.

## Local Drain Simulation

Local Docker can simulate a pod drain with:

- `GET /operations/drain`
- `POST /operations/drain/start`
- `POST /operations/drain/stop`

Starting drain marks the runtime unavailable for new sessions and emits
`runtime_draining_started`. Stopping drain emits `runtime_draining_stopped`.
When `interrupt_active_sessions=true`, local drain stops active sessions and
emits `session_interrupted` for each call.

## Kubernetes Rollout Contract

The future Kubernetes rollout path should use:

- PodDisruptionBudget so all media capacity cannot disappear at once.
- `preStop` hook to start drain before process termination.
- `terminationGracePeriodSeconds` sized to the call drain timeout.
- readiness gates for `api_ready`, `sip_registered`, `media_ready`, and
  `not_draining`.
- separate deployments for API, media, workers, and task pollers.

## Guarantees

Future calls can fail over by routing only to ready media capacity.

Active RTP/WebRTC media is not transparently migrated with the current
AudioSocket/peer-connection design. If the media owner disappears or drain
timeout is exceeded, the session is marked interrupted.

Background work can recover through durable queues, session leases, and
subagent task state. Late task results are stored for transcript/audit and must
not be spoken into closed calls.
