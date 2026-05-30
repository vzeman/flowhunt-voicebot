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
It uses a `VoiceActivityDetector` interface for level/activity decisions; the
default implementation is `RmsVoiceActivityDetector`. A future model-based VAD
can implement the same `detect(samples, threshold=...)` contract without
changing endpointing, barge-in, or turn-completion logic.

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

- audio below `barge_in_threshold` is treated as likely bot echo and ignored;
- audio at or above `barge_in_threshold` can start a caller turn and should
  interrupt playback.

The lower `start_threshold` still controls ordinary speech start detection when
playback is not active. Browser echo cancellation, the playback echo tail, and
future echo-cancellation stages still carry part of the self-audio suppression
responsibility, but live playback gating uses the stronger barge-in threshold.

The echo-tail window after playback no longer suppresses all caller input. It
suppresses quiet audio below `barge_in_threshold`, but loud speech can still
start a turn. This prevents the bot from ignoring a real caller who begins
speaking immediately after or during bot audio.

SIP and WebRTC sessions also guard the output queue against stale responses. If
a non-startup agent response is ready to play after a newer
`user_speech_started` or `user_transcript` event already exists, the session
drops that response instead of speaking it. The runtime emits
`agent_response_dropped` with reason `stale_response_after_new_caller_speech`.
Startup greetings are exempt so a greeting can still play after the initial
call setup and recording path settle.

When a TTS provider exposes `synthesize_stream`, SIP and WebRTC sessions queue
the first audio chunk as soon as it is available instead of waiting for the full
response to synthesize. If the caller starts speaking while later chunks are
still being generated, the remaining chunks are dropped and the old response is
not resumed.

The OpenAI-compatible TTS adapter uses the Speech API streaming response with
raw `pcm` output, converts the 24 kHz PCM stream into normalized call audio, and
resamples it to the runtime call sample rate before playback.

Before completed caller audio is sent to STT, the runtime trims trailing silence
that was only needed for endpointing while keeping a short tail for recognition
context. This reduces uploaded audio duration and avoids asking the STT provider
to transcribe long silence at the end of every turn.

## Configuration

`TurnDetectionConfig` contains:

- `sample_rate`
- `start_threshold`
- `stop_threshold`
- `vad_start_ms`
  - Default: `60`. This keeps the speech-start confirmation short enough for
    responsive barge-in while still rejecting single-frame noise spikes.
- `silence_ms`
  - Default: `450`. This endpointing delay favors lower latency for phone
    turns while leaving enough trailing silence for STT.
- `min_seconds`
  - Default: `0.35`.
- `max_seconds`
- `barge_in_threshold`
  - Default: `0.08`.

Configuration is validated when constructed. Sample rate, silence, and max turn
duration must be positive; stop threshold cannot exceed start threshold; max
turn duration cannot be shorter than the minimum; and barge-in threshold cannot
be negative. Barge-in threshold is intentionally independent from the normal
speech-start threshold so callers can interrupt more easily than they can start
a new turn from silence. Invalid values should fail startup or voicebot config
activation instead of producing unstable turn detection.
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

## Jitter Buffer

`AudioJitterBuffer` is a transport-neutral buffer for normalized float32 audio
blocks. `JitterBufferConfig` defines the sample rate, frame size, target delay,
and maximum delay. The buffer waits until it has the target delay plus one frame
before emitting fixed-size frames, trims oldest samples when the maximum delay is
exceeded, and exposes buffered sample/duration diagnostics. This gives SIP,
WebRTC, and future transports a shared primitive before turn detection without
mixing jitter handling into agent or STT logic.

The WebRTC receive path now feeds remote audio through `AudioJitterBuffer`
before VAD. Runtime settings:

- `VOICEBOT_WEBRTC_JITTER_BUFFER_ENABLED=true|false`
- `VOICEBOT_WEBRTC_JITTER_TARGET_DELAY_MS=60`
- `VOICEBOT_WEBRTC_JITTER_MAX_DELAY_MS=200`

The SIP AudioSocket receive path uses the same jitter buffer primitive before
its VAD loop. Runtime settings:

- `VOICEBOT_AUDIOSOCKET_JITTER_BUFFER_ENABLED=true|false`
- `VOICEBOT_AUDIOSOCKET_JITTER_TARGET_DELAY_MS=60`
- `VOICEBOT_AUDIOSOCKET_JITTER_MAX_DELAY_MS=200`

Direct unit-test calls to the low-level VAD block processors can still bypass
the jitter buffer; live WebRTC tracks and SIP AudioSocket frames use
`process_remote_audio_block()` so packet timing stabilization stays
transport-local.

## Debug Capture

`DebugAudioCapture` is a gated in-memory ring buffer for recent normalized audio
blocks. When disabled, `append()` is a no-op. When enabled, it keeps only the
configured number of seconds and exposes a small summary with sample count and
duration. Capture settings reject non-positive sample rates and negative
retention windows so diagnostics cannot report misleading durations. This gives
runtime diagnostics a shared primitive for debug audio capture without retaining
caller audio by default.

## Next Integration Steps

1. Replace duplicated SIP and WebRTC VAD loops with `TurnDetector`.
2. Emit metrics for every turn decision from `TurnDetectionResult.metric_data()`.
3. Add a pluggable VAD provider interface.
4. Replace duplicated SIP and WebRTC VAD loops with `TurnDetector`.
5. Wire debug capture behind runtime/workspace configuration.
6. Add stronger echo suppression strategy for real deployments.
