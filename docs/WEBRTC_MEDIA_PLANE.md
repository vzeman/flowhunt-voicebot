# WebRTC Media Plane Scale Contract

The local WebRTC inference console is embedded in the internal dashboard at
`/dashboard`. There is no standalone `/webrtc/test` route. The console uses the
voicebot API process to terminate browser offer/answer signaling with `aiortc`.

## Kubernetes Target

Production WebRTC needs workspace-scoped signaling, sticky routing to the media
worker that owns the peer connection, and reliable ICE/TURN infrastructure. The
initial target can remain self-hosted, but the contract leaves room for a later
managed media-server integration such as a LiveKit-style or Daily-style stack.

WebRTC routing scope is:

```text
workspace_id -> voicebot_id -> channel_id
```

The created `session_id` must route back to the owning media worker until the
session is closed or interrupted.

## Admission

The API should check workspace/voicebot capacity before allocating an
`RTCPeerConnection`. If there is no media capacity, the call should be rejected
or redirected before browser media resources and model/provider resources are
reserved.

`POST /routing/admission` implements the routed preflight for WebRTC widgets.
When capacity is unavailable, the fallback contract is a structured HTTP error
before SDP answer, so the browser can show a user-friendly unavailable state
without creating a peer connection.

## ICE And Secrets

STUN can be environment configured for local and staging use. TURN is required
for production reliability. TURN credentials must be represented as
workspace/voicebot secret references and must never be returned raw to clients.

## Reconnect And Failure

Browser network changes can be recovered with reconnect or ICE restart when the
session owner is still alive. Pod loss should be treated as interrupted media,
not transparent active media migration. Non-media work can continue through
session leases, worker queues, and subagent task state.

## Quality Metrics

Production observability should include:

- ICE state
- connection state
- packet loss
- jitter
- RTT
- audio level
- disconnect reason

The machine-readable contract is exposed at:

`GET /webrtc/media-plane`
