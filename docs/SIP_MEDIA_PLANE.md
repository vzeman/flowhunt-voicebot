# SIP/Asterisk Media Plane HA

This document defines the production direction for SIP/Asterisk media handling
while keeping the current local Docker path supported.

## Local Docker

Local development runs a single Asterisk container. It registers directly to SIP
providers with PJSIP, receives inbound calls, and hands media to the voicebot
through AudioSocket. Dynamic trunks remain stored in local JSON and rendered to
the Asterisk PJSIP include file.

This mode is intentionally simple and remains supported for development and
single-machine testing.

## Kubernetes Target

Production should use a SIP edge layer in front of disposable Asterisk media
workers. The SIP edge can be Kamailio/OpenSIPS or an equivalent FlowHunt-managed
edge service. The edge owns provider-facing routing, health-aware INVITE
placement, and provider-specific trunk registration behavior. Asterisk workers
own RTP and AudioSocket media only for sessions routed to them.

Trunk registration must be workspace scoped:

```text
workspace_id -> voicebot_id -> trunk_id
```

Some providers support active/active registrations, while others require
active/passive ownership. The registration strategy must be stored per trunk and
must not be hardcoded globally.

## Failover Boundary

Future calls can fail over by routing only to ready media capacity. Active RTP
calls cannot be assumed to survive Asterisk or pod loss with the current
AudioSocket design. If the media owner disappears, the session is marked
interrupted and non-media work can recover elsewhere through session leases,
worker queues, transcripts, and subagent task state.

## Readiness

The media plane distinguishes:

- `api_ready`: the voicebot API can serve control-plane requests.
- `sip_registered`: the trunk is registered and owned by the media plane.
- `media_ready`: Asterisk and AudioSocket can accept routed calls.
- `draining`: existing calls may continue, but new calls should not route here.

## Draining

Draining removes the node from ready routing before unregistering or disabling
trunks. Existing calls may finish if the pod remains alive long enough. New
calls route to another ready media worker or to the configured overflow/reject
policy.

`POST /routing/admission` implements the routed preflight for SIP trunks. When
capacity is unavailable, the fallback contract includes busy/unavailable SIP
responses and optional transfer to a configured fallback extension or human
queue.

## Call Control

Call control actions such as hangup, transfer, DTMF, and playback interruption
must route to the session lease owner. This keeps API workers, communication
agents, and SIP media workers independently scalable.

The machine-readable contract is exposed at:

`GET /sip/media-plane`
