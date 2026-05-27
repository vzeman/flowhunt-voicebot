#!/usr/bin/env python3
"""
Listen through the local microphone, transcribe each spoken phrase with Whisper,
then speak the recognized text back with Supertone Supertonic 3.

This is a small local demo. It records until speech is followed by a configurable
period of silence, so you can keep the script running and speak one phrase at a
time.
"""

from __future__ import annotations

import argparse
import queue
import tempfile
import threading
import time

import numpy as np
import sounddevice as sd
import whisper
from scipy.io import wavfile
from supertonic import TTS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local speech-to-text-to-speech demo.")
    parser.add_argument("--whisper-model", default="base", help="Whisper model name, e.g. tiny, base, small, medium, turbo.")
    parser.add_argument("--language", default=None, help="Optional Whisper/Supertonic language code, e.g. en, sk, de.")
    parser.add_argument("--voice", default="M1", help="Supertonic preset voice name, e.g. M1.")
    parser.add_argument("--input-device", default=None, help="Optional sounddevice input device name or index.")
    parser.add_argument("--output-device", default=None, help="Optional sounddevice output device name or index.")
    parser.add_argument("--sample-rate", type=int, default=16_000, help="Microphone recording sample rate.")
    parser.add_argument("--block-ms", type=int, default=100, help="Audio callback block size in milliseconds.")
    parser.add_argument("--start-threshold", type=float, default=0.018, help="RMS level that starts recording speech.")
    parser.add_argument("--stop-threshold", type=float, default=0.010, help="RMS level treated as silence after speech starts.")
    parser.add_argument("--barge-in-threshold", type=float, default=0.060, help="Maximum RMS level required to interrupt while the script is speaking.")
    parser.add_argument("--barge-in-ratio", type=float, default=1.7, help="Interrupt when mic RMS is this multiple of the measured speaker bleed.")
    parser.add_argument("--barge-in-margin", type=float, default=0.010, help="Interrupt when mic RMS exceeds measured speaker bleed by this margin.")
    parser.add_argument("--echo-tail-ms", type=int, default=500, help="Ignore mic input this long after generated speech stops.")
    parser.add_argument("--print-levels", action="store_true", help="Print microphone RMS levels and dynamic thresholds for tuning.")
    parser.add_argument("--silence-ms", type=int, default=900, help="Silence duration that ends an utterance.")
    parser.add_argument("--pre-roll-ms", type=int, default=300, help="Audio to keep before speech starts, to avoid clipped first words.")
    parser.add_argument("--max-seconds", type=float, default=20.0, help="Maximum length of one utterance.")
    parser.add_argument("--min-seconds", type=float, default=0.5, help="Ignore shorter recordings.")
    parser.add_argument("--keep-wavs", action="store_true", help="Keep generated input/output wav files in the temp directory.")
    return parser.parse_args()


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(block), dtype=np.float64)))


class TokenState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def bump(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    def current(self) -> int:
        with self._lock:
            return self._value


class InterruptiblePlayer:
    def __init__(self, sample_rate: int, output_device: str | int | None) -> None:
        self.sample_rate = sample_rate
        self.output_device = output_device
        self._lock = threading.Lock()
        self._samples: np.ndarray | None = None
        self._position = 0
        self._last_playback_time = 0.0
        self._stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=2,
            dtype="float32",
            device=output_device,
            callback=self._callback,
        )

    def __enter__(self) -> "InterruptiblePlayer":
        self._stream.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()
        self._stream.stop()
        self._stream.close()

    def _callback(self, outdata: np.ndarray, frames: int, callback_time, status: sd.CallbackFlags) -> None:
        if status:
            print(f"Output status: {status}")

        outdata.fill(0)
        with self._lock:
            if self._samples is None:
                return

            remaining = len(self._samples) - self._position
            if remaining <= 0:
                self._samples = None
                self._position = 0
                self._last_playback_time = time.monotonic()
                return

            count = min(frames, remaining)
            chunk = self._samples[self._position : self._position + count]
            outdata[:count, 0] = chunk
            outdata[:count, 1] = chunk
            self._position += count
            self._last_playback_time = time.monotonic()

            if self._position >= len(self._samples):
                self._samples = None
                self._position = 0
                self._last_playback_time = time.monotonic()

    def play(self, samples: np.ndarray) -> None:
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        with self._lock:
            self._samples = samples
            self._position = 0

    def stop(self) -> None:
        with self._lock:
            self._samples = None
            self._position = 0
            self._last_playback_time = time.monotonic()

    def is_playing(self) -> bool:
        with self._lock:
            return self._samples is not None

    def recently_played(self, tail_seconds: float) -> bool:
        with self._lock:
            return (time.monotonic() - self._last_playback_time) < tail_seconds


def listen_forever(
    args: argparse.Namespace,
    utterance_queue: queue.Queue[tuple[int, np.ndarray]],
    token_state: TokenState,
    player: InterruptiblePlayer,
) -> None:
    audio_queue: queue.Queue[np.ndarray] = queue.Queue()
    block_size = int(args.sample_rate * args.block_ms / 1000)
    silence_blocks_needed = max(1, int(args.silence_ms / args.block_ms))
    max_blocks = max(1, int(args.max_seconds * 1000 / args.block_ms))
    pre_roll_blocks_needed = max(0, int(args.pre_roll_ms / args.block_ms))
    echo_tail_seconds = max(0.0, args.echo_tail_ms / 1000)

    def callback(indata: np.ndarray, frames: int, callback_time, status: sd.CallbackFlags) -> None:
        if status:
            print(f"Audio status: {status}")
        audio_queue.put(indata.copy())

    print("Listening. Speak now, or press Ctrl+C to stop.")
    is_recording = False
    silence_blocks = 0
    current_token = 0
    collected: list[np.ndarray] = []
    pre_roll: list[np.ndarray] = []
    blocks_seen = 0
    echo_floor = args.start_threshold
    last_level_print = 0.0

    with sd.InputStream(
        samplerate=args.sample_rate,
        channels=1,
        dtype="float32",
        blocksize=block_size,
        device=args.input_device,
        callback=callback,
    ):
        while True:
            block = audio_queue.get()
            level = rms(block)
            playback_active = player.is_playing()
            in_echo_tail = player.recently_played(echo_tail_seconds)
            now = time.monotonic()

            if not is_recording:
                dynamic_barge_threshold = max(
                    args.start_threshold,
                    min(
                        args.barge_in_threshold,
                        max(echo_floor * args.barge_in_ratio, echo_floor + args.barge_in_margin),
                    ),
                )

                if args.print_levels and now - last_level_print >= 0.5:
                    mode = "playback" if playback_active else "listen"
                    print(
                        f"level={level:.4f} mode={mode} "
                        f"echo_floor={echo_floor:.4f} barge={dynamic_barge_threshold:.4f}"
                    )
                    last_level_print = now

                if playback_active and level < dynamic_barge_threshold:
                    echo_floor = 0.9 * echo_floor + 0.1 * level
                    continue

                if in_echo_tail and level < args.barge_in_threshold:
                    continue

                threshold = dynamic_barge_threshold if playback_active else args.start_threshold
                if level >= threshold:
                    current_token = token_state.bump()
                    player.stop()
                    is_recording = True
                    silence_blocks = 0
                    collected = [*pre_roll, block]
                    pre_roll = []
                    blocks_seen = 1
                    if playback_active:
                        print("Recording... interrupted generated speech.")
                    else:
                        print("Recording...")
                else:
                    if pre_roll_blocks_needed > 0:
                        pre_roll.append(block)
                        pre_roll = pre_roll[-pre_roll_blocks_needed:]
                continue

            collected.append(block)
            blocks_seen += 1

            if level < args.stop_threshold:
                silence_blocks += 1
            else:
                silence_blocks = 0

            if silence_blocks < silence_blocks_needed and blocks_seen < max_blocks:
                continue

            audio = np.concatenate(collected, axis=0).reshape(-1)
            duration = len(audio) / args.sample_rate

            is_recording = False
            silence_blocks = 0
            collected = []
            blocks_seen = 0

            if duration < args.min_seconds:
                print(f"Ignored short audio: {duration:.2f}s")
                continue

            utterance_queue.put((current_token, audio))
            print(f"Queued speech ({duration:.2f}s). Listening continues.")


def transcribe_audio(model, audio: np.ndarray, sample_rate: int, language: str | None, keep_wavs: bool) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=not keep_wavs) as tmp:
        wavfile.write(tmp.name, sample_rate, np.clip(audio, -1.0, 1.0))
        result = model.transcribe(tmp.name, language=language, fp16=False)
        return str(result.get("text", "")).strip()


def synthesize(tts: TTS, text: str, voice_style, language: str | None, keep_wavs: bool) -> tuple[np.ndarray, float]:
    synth_kwargs = {"voice_style": voice_style}
    if language:
        synth_kwargs["lang"] = language

    wav, duration = tts.synthesize(text, **synth_kwargs)
    duration_seconds = float(np.asarray(duration).reshape(-1)[0])
    samples = np.asarray(wav, dtype=np.float32).squeeze()

    if keep_wavs:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tts.save_audio(wav, tmp.name)
        tmp.close()

    return samples, duration_seconds


def speech_worker(
    utterance_queue: queue.Queue[tuple[int, np.ndarray]],
    token_state: TokenState,
    player: InterruptiblePlayer,
    whisper_model,
    tts: TTS,
    voice_style,
    args: argparse.Namespace,
) -> None:
    while True:
        token, audio = utterance_queue.get()

        while True:
            try:
                token, audio = utterance_queue.get_nowait()
            except queue.Empty:
                break

        started = time.perf_counter()
        text = transcribe_audio(whisper_model, audio, args.sample_rate, args.language, args.keep_wavs)
        elapsed = time.perf_counter() - started

        if token != token_state.current():
            print("Discarded old transcription because newer speech started.")
            continue

        if not text:
            print("No text recognized.")
            continue

        print(f"You said ({elapsed:.2f}s): {text}")

        samples, duration_seconds = synthesize(tts, text, voice_style, args.language, args.keep_wavs)
        if token != token_state.current():
            print("Discarded old speech because newer speech started.")
            continue

        print(f"Speaking back ({duration_seconds:.2f}s). Start talking to interrupt.")
        player.play(samples)


def main() -> None:
    args = parse_args()

    if args.input_device is not None and str(args.input_device).isdigit():
        args.input_device = int(args.input_device)
    if args.output_device is not None and str(args.output_device).isdigit():
        args.output_device = int(args.output_device)

    print(f"Loading Whisper model: {args.whisper_model}")
    whisper_model = whisper.load_model(args.whisper_model)

    print("Loading Supertonic 3 TTS. First run may download model files from Hugging Face.")
    tts = TTS(auto_download=True)
    voice_style = tts.get_voice_style(voice_name=args.voice)

    utterance_queue: queue.Queue[tuple[int, np.ndarray]] = queue.Queue()
    token_state = TokenState()

    with InterruptiblePlayer(tts.sample_rate, args.output_device) as player:
        worker = threading.Thread(
            target=speech_worker,
            args=(utterance_queue, token_state, player, whisper_model, tts, voice_style, args),
            daemon=True,
        )
        worker.start()
        listen_forever(args, utterance_queue, token_state, player)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
