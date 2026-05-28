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

## Capabilities

Call control must be explicit per transport. The first capability sets are:

- Asterisk AudioSocket: `hangup`, `transfer`, `send_dtmf`, `stop_playback`,
  `read_transcript`
- WebRTC: `hangup`, `stop_playback`, `read_transcript`

Unsupported actions return a failed `CallControlResult` with a reason and
transport name. Runtime code should emit that result as a call-control event
instead of throwing transport-specific errors into the agent loop.

Capabilities are also exposed as a workspace-scoped catalog:

`GET /workspaces/{workspace_id}/voicebots/{voicebot_id}/transports`

The response lists each known transport kind, whether that transport is
implemented by this runtime, call-control actions, audio flags, concurrency
support, and modality support. Control planes can use this before binding a
voicebot to SIP, WebRTC, or future telephony providers.

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
