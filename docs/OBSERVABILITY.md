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

Realtime audio metrics include `tts_first_audio_latency_seconds`; transports or
workers may also emit `end_of_speech_to_playback_started_seconds` for full
caller-finished to first-bot-audio latency. Metric summaries include `min`,
`max`, `avg`, `p50`, `p90`, and the latest sample. These are consumed by
`/metrics`, `/scaling/signals`, `/observability/timeline`, and
`/observability/slo`.

Streaming-RAG turns add stage metrics so speculative work can be compared with
final-transcript-only behavior:

- `partial_stt_first_text_seconds`
- `partial_stt_to_speculative_start_seconds`
- `speech_start_to_speculative_start_seconds`
- `speech_finished_to_final_transcript_seconds`
- `speculative_task_completed_before_final_transcript`
- `speculative_result_reuse_latency_seconds`
- `agent_task_pickup_latency_seconds`
- `agent_stream_first_text_latency_seconds`
- `tts_stream_first_audio_latency_seconds`
- `response_request_to_first_playback_seconds`

Timeline latency summaries also report Streaming-RAG outcome counts for
confirmed, cancelled, and superseded speculative tasks, plus the confirmation
hit rate and reflector decision counts.

## Latency Benchmark

Run the deterministic local benchmark without external credentials:

```bash
python3 -m tools.latency_benchmark
```

It emits JSON for final-only, final tool-only, speculative confirmed,
speculative cancelled, speculative superseded, and streaming agent/TTS
scenarios. The benchmark checks that confirmed speculative work reduces final
turn wait, speculative work starts before endpointing, unconfirmed work is not
spoken, and superseded candidates are reported.

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
