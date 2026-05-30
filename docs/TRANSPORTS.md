# Transport and Media Abstraction

The transport layer owns how a caller reaches the voicebot. The session
pipeline should receive normalized audio, lifecycle events, route metadata, and
call-control results without knowing whether the source is SIP, WebRTC, or a
future provider.

## Session Descriptor

Each inbound call is represented by a `MediaSessionDescriptor`:

- `call_id`: runtime call/session id used by events, transcript, and tools
- `transport`: transport kind such as `asterisk_audiosocket` or `webrtc`
- `route`: resolved `workspace_id`, `voicebot_id`, optional trunk/provider ids,
  and any remaining provider metadata
- `capabilities`: declared call-control and media features
- `sample_rate`: normalized inbound media rate

The descriptor has `lifecycle_event_data()` so `call_started` and
`call_connected` events can carry the same route and transport fields for every
transport.

Runtime sessions add `pipeline_version` from the canonical pipeline contract to
their lifecycle events and snapshots. SIP and WebRTC both map to the same stage
sequence exposed at `GET /pipeline/contract`; transport-specific behavior stays
behind the media adapters at the first and last pipeline stages.

Descriptors require a non-empty `call_id`, supported transport kind, and
positive `sample_rate`. Transport factories should fail fast on invalid media
rates or unsupported transport kinds before creating runtime sessions or
emitting lifecycle events.

`MediaSessionDescriptor.require_workspace_scope()` converts routed transport
metadata into a `WorkspaceScope` and fails if `workspace_id`, `voicebot_id`, or
session id are missing. Use it when a transport path is about to create
workspace-scoped sessions, events, transcripts, or delegated tasks.

## Capabilities

Call control must be explicit per transport. The first capability sets are:

- Asterisk AudioSocket: `hangup`, `transfer`, `send_dtmf`, `stop_playback`,
  `read_transcript`
- WebRTC: `hangup`, `stop_playback`, `read_transcript`

Unsupported actions return a failed `CallControlResult` with a reason and
transport name. Runtime code should emit that result as a call-control event
instead of throwing transport-specific errors into the agent loop.
Capability declarations reject unknown call-control actions when the transport
descriptor is created.

`CallControlRequest.as_event_data()` and `CallControlResult.as_event_data()`
provide normalized event payloads for `call_control_requested` and
`call_control_completed`. Runtime integrations should use these helpers so SIP,
WebRTC, and future transports report success/failure in the same shape.

Capabilities are also exposed as a workspace-scoped catalog:

`GET /workspaces/{workspace_id}/voicebots/{voicebot_id}/transports`

The response lists each known transport kind, whether that transport is
implemented by this runtime, call-control actions, audio flags, concurrency
support, and modality support. Control planes can use this before binding a
voicebot to SIP, WebRTC, or future telephony providers.

## SIP Runtime

SIP calls are currently handled by Asterisk using PJSIP registration, INVITE,
RTP, and AudioSocket media handoff into the voicebot service. Asterisk loads a
base PJSIP configuration from `asterisk/docker-entrypoint.sh` and includes
`/data/asterisk/pjsip-trunks.conf` for runtime trunks.

Dynamic trunks are managed by the voicebot HTTP API and persisted in
`/data/sip_trunks.json`. For local development only, the Asterisk entrypoint can
seed a single `trunk-default` registration from `SIP_HOST`, `SIP_USER`, and
`SIP_PASSWORD` when the generated include file does not exist or is empty. Once
the include exists, the dynamic API path owns it.

The PJSIP UDP transport bind is controlled by `PJSIP_BIND`, defaulting to
`0.0.0.0:5060`. Local Docker/NAT environments can require a different source
port for REGISTER responses to arrive reliably; set
`PJSIP_BIND=0.0.0.0:5062` or another free UDP port and recreate the Asterisk
container when that is needed.

## Routing

Routing metadata uses FlowHunt workspace terminology:

- `workspace_id`
- `voicebot_id`
- `trunk_id`
- `external_call_id`

This avoids adding a separate tenant model. One workspace can own multiple
voicebots, and every call session can be routed to the configured voicebot using
the descriptor route.

## Next Integration Step

The current implementation introduces the shared contract in
`voicebot/transports.py`. The next step is to make SIP/Asterisk and WebRTC build
descriptors at session creation and emit lifecycle events from those
descriptors, then move transport-specific call-control execution behind the same
result contract.

WebRTC sessions now build a `MediaSessionDescriptor` at session creation and use
it for lifecycle event payloads and session snapshots. SIP/Asterisk AudioSocket
sessions also build descriptors when the AudioSocket UUID is received and use
descriptor data for lifecycle events and snapshots. Workspace/voicebot routing
for SIP still depends on moving trunk bindings behind workspace-scoped channel
resolution. Transport capabilities are now discoverable through the shared API
catalog, so clients do not need to infer support from live sessions.
