# Security, Isolation, Audit, And Retention

FlowHunt workspaces are the isolation boundary for the voicebot runtime. Local
Docker development remains permissive by default, but production deployment must
enable workspace authorization and shared storage policies before serving more
than one workspace.

## Modes

`GET /security/contract` returns the active security contract:

- `mode=local_permissive` when `VOICEBOT_DEPLOYMENT_MODE` is `local`,
  `development`, `dev`, or `test`.
- `mode=production_enforced` for other deployment modes.

Outside local mode, `VOICEBOT_WORKSPACE_ACCESS_CONTROL_ENABLED=true` is
required. If it is disabled, `GET /health/readiness` reports a failing
`security_contract` check.

Local allow-list enforcement is configured with:

- `VOICEBOT_WORKSPACE_ACCESS_CONTROL_ENABLED=true`
- `VOICEBOT_ALLOWED_WORKSPACE_IDS=workspace-1,workspace-2`

In FlowHunt production, this allow-list contract should be replaced by the
backend workspace permission layer while preserving the same deny-by-default
runtime behavior.

## Workspace Isolation

Workspace-scoped product APIs use `/workspaces/{workspace_id}/...` and call the
workspace access policy before reading or mutating state. Session timelines,
transcripts, external tasks, provider config, runtime config, and channel lists
are filtered by workspace and voicebot IDs.

Internal runtime endpoints that are not workspace-scoped are documented as
internal contracts. They must only be reachable from authenticated FlowHunt
backend services or runtime workers in production network policy.

## Secret Handling

Secrets are never returned raw by runtime APIs. Provider config and SIP trunk
responses use secret references or redacted metadata:

```json
{
  "password": {
    "configured": true,
    "redacted": true
  }
}
```

Security audit payloads are recursively redacted for keys containing markers
such as `api_key`, `authorization`, `password`, `secret`, or `token`.

Local `.env` secrets are acceptable for Docker testing only. Production should
store provider credentials, SIP passwords, and webhook credentials as
workspace-scoped secret references managed by FlowHunt.

## Audit Events

Security-sensitive actions emit `security_audit` events with redacted payloads.
The current runtime records:

- call-control requests such as hangup, transfer, and DTMF
- provider config saves
- runtime config saves
- transcript reads through workspace session routes
- SIP trunk create/update/connect/disconnect/delete actions
- explicit internal audit submissions through
  `POST /workspaces/{workspace_id}/security/audit`

Audit events include the workspace ID when it is known. Legacy local SIP trunk
commands currently do not have a workspace route; production channel APIs must
bind trunks to workspace and voicebot records before exposing them to users.

## Retention

`GET /workspaces/{workspace_id}/security/retention` returns the active retention
classes and deletion hooks for that workspace. The contract covers:

- events
- transcripts
- recordings and debug audio
- cached TTS audio
- subagent task records

The local implementation exposes the policy and keeps data in JSON/filesystem
stores. Production storage must implement deletion by workspace, then narrower
voicebot/session/artifact scope when supplied.

## PII-Safe Logging

`VOICEBOT_PII_SAFE_LOGGING_ENABLED=true` is the default. Diagnostics and SLO
endpoints do not include transcript text. Debug audio capture remains disabled
by default and should be enabled only for explicit support investigations.

## Production Network Policy Assumptions

Before Kubernetes deployment, network policy should restrict:

- Asterisk/AMI and SIP control access to voicebot media/control workers.
- Redis or queue access to runtime and lifecycle workers.
- Database access to FlowHunt backend services and runtime workers using
  workspace-scoped queries.
- Provider egress to provider adapter workers.
- Internal runtime APIs to authenticated FlowHunt backend services and worker
  identities.

External webhook/API inputs must validate JSON content type, bounded payload
size, an internal identity or signature, and workspace route consistency.
