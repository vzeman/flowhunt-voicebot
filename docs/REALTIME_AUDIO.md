# Realtime Audio Foundation

This document describes the realtime audio foundation introduced for issue #81.

The current SIP and WebRTC sessions still contain their existing media loops, but new work should move toward shared audio primitives from `voicebot.realtime_audio`.

## Goals

- Keep VAD and turn detection replaceable.
- Make barge-in behavior deterministic.
- Prevent bot playback audio from becoming user speech.
- Keep audio decisions observable and testable without live calls.
- Support SIP and WebRTC through the same turn detection semantics.

## Turn Detection

`TurnDetector` processes normalized float32 audio blocks and returns a `TurnDetectionResult`.

Inputs:

- audio block
- sample rate
- playback active flag
- echo suppression flag

Outputs:

- decision
- level
- block duration
- speech started flag
- speech finished flag
- playback interruption flag
- completed turn audio

`TurnDetectionResult.metric_data()` returns JSON-ready VAD telemetry for metrics
events. It includes decision, level, block duration, started/finished flags,
barge-in interruption, turn duration, and optional session/turn identifiers.

The SIP AudioSocket and WebRTC runtime loops emit `vad_decision` metrics for
speech start and terminal turn decisions, plus `speech_duration_seconds` and
`silence_duration_seconds` when a turn closes. This keeps live-call behavior
debuggable without requiring packet-level audio logs.

Decision values:

- `ignored`: input was suppressed, usually bot playback echo.
- `silence`: below speech start threshold.
- `pending_start`: possible speech, waiting for configured start duration.
- `speech_started`: caller speech turn started.
- `speech_continues`: active speech turn continues.
- `speech_finished`: speech turn completed and is long enough for STT.
- `speech_too_short`: speech ended but is below minimum duration.

## Barge-In

When playback is active:

- audio below `start_threshold` is treated as likely bot echo and ignored;
- audio above `start_threshold` can start a caller turn and should interrupt
  playback, even if it is below the higher diagnostic `barge_in_threshold`.

The higher `barge_in_threshold` remains useful for diagnostics and future
confidence scoring, but live interruption must favor the caller. Browser echo
cancellation, the playback echo tail, and future echo-cancellation stages carry
the main self-audio suppression responsibility.

## Configuration

`TurnDetectionConfig` contains:

- `sample_rate`
- `start_threshold`
- `stop_threshold`
- `vad_start_ms`
- `silence_ms`
  - Default: `700`. This endpointing delay is intentionally below one second
    so short phone turns are not held unnecessarily before STT starts.
- `min_seconds`
- `max_seconds`
- `barge_in_threshold`

Configuration is validated when constructed. Sample rate, silence, and max turn
duration must be positive; stop threshold cannot exceed start threshold; max
turn duration cannot be shorter than the minimum; and barge-in threshold cannot
be below the start threshold. Invalid values should fail startup or voicebot
config activation instead of producing unstable turn detection.
When playback is active, the detector ignores audio below `barge_in_threshold`
instead of the lower speech start threshold. That keeps generated speech or
speaker echo from retriggering the bot while still allowing a louder caller
interruption to stop playback.

These values should be resolved per voicebot/session once workspace-based configuration is implemented.

`turn_detection_config_from_settings(settings, sample_rate)` maps the current
runtime settings object into this shared config. Transport code should use this
factory when constructing turn detectors so SIP, WebRTC, and future media
ingress paths do not duplicate threshold and timing mapping.

## Chunk Normalization

`AudioChunkNormalizer` handles transport-boundary normalization:

- integer PCM to float32 in `[-1.0, 1.0]`
- oversized float values scaled down from PCM-like ranges
- mono/stereo downmixing
- sample-rate conversion
- validation of source rate, target rate, and channel count

Transport code should normalize audio before feeding it into the turn detector
so SIP, WebRTC, and future providers share the same VAD behavior.

## Debug Capture

`DebugAudioCapture` is a gated in-memory ring buffer for recent normalized audio
blocks. When disabled, `append()` is a no-op. When enabled, it keeps only the
configured number of seconds and exposes a small summary with sample count and
duration. This gives runtime diagnostics a shared primitive for debug audio
capture without retaining caller audio by default.

## Next Integration Steps

1. Replace duplicated SIP and WebRTC VAD loops with `TurnDetector`.
2. Emit metrics for every turn decision from `TurnDetectionResult.metric_data()`.
3. Add a pluggable VAD provider interface.
4. Add jitter buffer and audio normalization stages before turn detection.
5. Wire debug capture behind runtime/workspace configuration.
6. Add stronger echo suppression strategy for real deployments.
