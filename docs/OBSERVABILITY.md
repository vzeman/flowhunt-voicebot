# Observability

The runtime exposes event timelines, metrics, SLO checks, and support-safe
diagnostics for local Docker today and future production monitoring.

## Correlation

Structured diagnostics use these fields when available:

- `trace_id`
- `workspace_id`
- `voicebot_id`
- `session_id`
- `call_id`
- `turn_id`
- `event_id`

Routine diagnostics are secret-safe and do not include transcript text. Enable
raw transcript/event inspection only through explicit transcript or event APIs
for debugging.

## Endpoints

- `GET /metrics`: aggregated metrics from recent events.
- `GET /scaling/signals`: autoscaling signal snapshot.
- `GET /scaling/signals?format=prometheus`: Prometheus text export for scaling
  signals.
- `GET /observability/timeline`: categorized event timeline with audio,
  provider, latency, health, and SLO summaries.
- `GET /observability/slo`: SLO checks for a filtered event slice.
- `GET /observability/diagnostics`: support-safe diagnostics and
  troubleshooting hints.
- `POST /observability/evaluate`: deterministic conversation checks for support
  and regression tests.

## SLO Candidates

Current SLO checks include:

- `call_to_greeting_audio_seconds`
- `speech_to_transcript_seconds`
- `end_of_speech_to_playback_started_seconds`
- `successful_call_setup_rate`
- `provider_error_rate`

Targets are intentionally explicit in the API response so production alerting
can map them to Prometheus, Datadog, OpenTelemetry, or FlowHunt-native monitors.

## Diagnostics

Diagnostics summarize:

- timeline health warnings
- provider failures
- slowest turn
- category counts
- SLO state
- support hints for STT no-text, playback interruption, provider failures, and
  slow response turns

This lets support/debug UI identify common causes of slow or missing responses
without manually comparing event timestamps.
