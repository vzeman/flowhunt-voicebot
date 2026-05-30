# Realtime Audio Foundation

This document describes the realtime audio foundation introduced for issue #81.

The current SIP and WebRTC sessions still contain their existing media loops, but new work should move toward shared audio primitives from `voicebot.realtime_audio`.

## Goals

- Keep VAD and turn detection replaceable.
- Make barge-in behavior deterministic.
- Prevent bot playback audio from becoming user speech.
- Keep audio decisions observable and testable without live calls.
- Support SIP and WebRTC through the same turn detection semantics.

The current profile is exposed at:

`GET /realtime/audio-profile`

The response includes turn-detection settings, cancellation capabilities,
streaming support, jitter/normalization settings, TTS cache status, and the
regression areas covered by tests.

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

The runtime records `tts_first_audio_latency_seconds` when the first TTS audio
chunk is available. Autoscaling and diagnostics also recognize
`end_of_speech_to_playback_started_seconds` when emitted by a transport or
worker, so STT, agent, TTS, and playback delay can be separated.

Communication-agent provider failures are treated as realtime failures, not
background job failures. OpenAI/compatible `server_error` and HTTP 500 model
turn failures are not retried inside the same live voice turn; the agent speaks
a short fallback while the call is active. Docker defaults keep the
communication-agent provider timeout at 8 seconds so a provider stall cannot
block the caller for a full minute. If that fallback speech cannot be delivered
because the call has already ended, the agent releases the claimed task instead
of renewing it indefinitely.

For ordinary caller requests, the communication agent also starts a delayed
progress acknowledgement timer while the model is deciding what to do. If no
answer or tool call is ready within roughly one second, the bot says a short
"I am checking that" message without marking the caller task as answered. When
the model later invokes a colleague/subagent tool, the wrapper suppresses the
tool's default progress phrase so the caller does not hear duplicate waiting
messages.

Colleague/subagent progress speech is intentionally scheduled in parallel with
the background work. The tool handler queues the spoken progress update as a
fire-and-forget call response and immediately continues to create or schedule
the FlowHunt/subagent task. Only call-control actions that depend on sequencing,
such as saying goodbye before hangup, wait for speech playback before executing
the control action.

The communication-agent tool executor separates speech intent from background
work intent when both are returned in the same model turn. `say` calls and
colleague/subagent work calls are dispatched concurrently; read-only tools still
complete before spoken follow-up, and call-control tools remain sequenced after
speech when an acknowledgement is required.

Completed colleague/subagent results are normalized before speech. The
communication layer strips provider greetings, internal task/status text,
markdown, links, duplicated progress wording, and raw provider payloads, then
turns the useful result into a short spoken answer. Delegated result speech is
budgeted to roughly one or two concise spoken sentences; longer detail remains
in the transcript/task context for follow-up questions instead of being read in
full. This keeps a FlowHunt or other colleague response from being read
literally when it contains content meant for chat or internal logs.

Completed colleague/subagent result speech is persistent until it is presented.
If the caller starts speaking while the result is being prepared, the runtime
defers it until silence instead of treating it like an ordinary stale answer.
Normal older answers are still dropped after newer caller activity, but
completed colleague results should not disappear just because the caller barged
in before the first playback attempt.

Before completed caller audio is sent to STT, the runtime trims trailing silence
that was only needed for endpointing while keeping a short tail for recognition
context. This reduces uploaded audio duration and avoids asking the STT provider
to transcribe long silence at the end of every turn.

STT jobs carry the interruption generation from the speech turn they belong to.
If the caller starts a newer turn before an older STT job finishes, the stale
transcript can still be recorded for audit with `stale=true`, but it does not
create a new `agent_response_requested` event. The runtime emits
`stt_result_dropped` so operators can see that a recognized utterance was
superseded by newer caller speech.

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
- communication-agent timeout
  - Docker default: `VOICEBOT_OPENAI_AGENT_TIMEOUT=8` and
    `VOICEBOT_ANTHROPIC_AGENT_TIMEOUT=8`.

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

1. Add optional model-based VAD/noise isolation behind the existing detector
   interface.
2. Propagate queue cancellation to remote provider SDK calls when supported.
3. Move TTS cache metadata to shared/object storage for multi-pod production.
4. Add audio fixture tests for more real-world telephony noise and echo cases.
5. Emit transport-provided first-audio and packet-level quality metrics where
   available.
