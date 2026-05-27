#!/usr/bin/env python3
"""
Asterisk AudioSocket bridge for Whisper -> Supertonic 3.

Run this Python service locally, then route a SIP call in Asterisk to:

    AudioSocket(<uuid>,127.0.0.1:9019)

Asterisk handles SIP/RTP. This process receives 8 kHz signed-linear call audio
over TCP, detects utterances, transcribes them with Whisper, synthesizes the
reply with Supertonic, and streams 8 kHz signed-linear audio back to the call.
"""

from __future__ import annotations

import argparse
import socket
import socketserver
import struct
import tempfile
import threading
import time
import uuid
from collections import deque

import numpy as np
import whisper
from scipy.io import wavfile
from scipy.signal import resample_poly
from supertonic import TTS


MSG_TERMINATE = 0x00
MSG_UUID = 0x01
MSG_DTMF = 0x03
MSG_SLIN8 = 0x10

CALL_SAMPLE_RATE = 8_000
WHISPER_SAMPLE_RATE = 16_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Whisper/Supertonic bridge for Asterisk AudioSocket.")
    parser.add_argument("--host", default="127.0.0.1", help="TCP host to bind.")
    parser.add_argument("--port", type=int, default=9019, help="TCP port to bind.")
    parser.add_argument("--whisper-model", default="base", help="Whisper model name.")
    parser.add_argument("--language", default=None, help="Optional language code, e.g. en, sk, de.")
    parser.add_argument("--voice", default="M1", help="Supertonic preset voice name.")
    parser.add_argument("--start-threshold", type=float, default=0.018, help="RMS level that starts recording speech.")
    parser.add_argument("--stop-threshold", type=float, default=0.010, help="RMS level treated as silence after speech starts.")
    parser.add_argument("--silence-ms", type=int, default=900, help="Silence duration that ends an utterance.")
    parser.add_argument("--min-seconds", type=float, default=0.5, help="Ignore shorter utterances.")
    parser.add_argument("--max-seconds", type=float, default=20.0, help="Maximum utterance duration.")
    parser.add_argument("--reply-packet-ms", type=int, default=20, help="Outgoing AudioSocket packet size.")
    parser.add_argument("--keepalive-ms", type=int, default=20, help="Silence packet interval while waiting for speech.")
    return parser.parse_args()


def rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples), dtype=np.float64)))


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError("socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_message(sock: socket.socket) -> tuple[int, bytes]:
    header = recv_exact(sock, 3)
    msg_type = header[0]
    payload_len = struct.unpack("!H", header[1:3])[0]
    payload = recv_exact(sock, payload_len) if payload_len else b""
    return msg_type, payload


def write_message(sock: socket.socket, msg_type: int, payload: bytes = b"") -> None:
    sock.sendall(bytes([msg_type]) + struct.pack("!H", len(payload)) + payload)


def pcm16_bytes_to_float32(payload: bytes) -> np.ndarray:
    return np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0


def float32_to_pcm16_bytes(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def resample_audio(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return samples.astype(np.float32, copy=False)

    divisor = np.gcd(source_rate, target_rate)
    up = target_rate // divisor
    down = source_rate // divisor
    return resample_poly(samples, up, down).astype(np.float32)


class SpeechEngine:
    def __init__(self, args: argparse.Namespace) -> None:
        print(f"Loading Whisper model: {args.whisper_model}")
        self.whisper_model = whisper.load_model(args.whisper_model)

        print("Loading Supertonic 3 TTS.")
        self.tts = TTS(auto_download=True)
        self.voice_style = self.tts.get_voice_style(voice_name=args.voice)
        self.language = args.language

        self._lock = threading.Lock()

    def transcribe(self, call_audio: np.ndarray) -> str:
        whisper_audio = resample_audio(call_audio, CALL_SAMPLE_RATE, WHISPER_SAMPLE_RATE)
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
            wavfile.write(tmp.name, WHISPER_SAMPLE_RATE, np.clip(whisper_audio, -1.0, 1.0))
            with self._lock:
                result = self.whisper_model.transcribe(tmp.name, language=self.language, fp16=False)
        return str(result.get("text", "")).strip()

    def synthesize_for_call(self, text: str) -> tuple[np.ndarray, float]:
        synth_kwargs = {"voice_style": self.voice_style}
        if self.language:
            synth_kwargs["lang"] = self.language

        with self._lock:
            wav, duration = self.tts.synthesize(text, **synth_kwargs)

        duration_seconds = float(np.asarray(duration).reshape(-1)[0])
        tts_audio = np.asarray(wav, dtype=np.float32).squeeze()
        call_audio = resample_audio(tts_audio, self.tts.sample_rate, CALL_SAMPLE_RATE)
        return call_audio, duration_seconds


class AudioSocketHandler(socketserver.BaseRequestHandler):
    engine: SpeechEngine
    args: argparse.Namespace

    def handle(self) -> None:
        call_id = "unknown"
        is_recording = False
        collected: list[np.ndarray] = []
        silence_ms = 0
        speech_ms = 0
        send_queue: deque[np.ndarray] = deque()
        send_lock = threading.Lock()
        stop_sender = threading.Event()

        sender = threading.Thread(
            target=self.send_audio_loop,
            args=(send_queue, send_lock, stop_sender),
            daemon=True,
        )
        sender.start()

        print(f"AudioSocket connected from {self.client_address}")

        try:
            while True:
                msg_type, payload = read_message(self.request)

                if msg_type == MSG_TERMINATE:
                    print(f"Call {call_id}: terminate")
                    return

                if msg_type == MSG_UUID:
                    call_id = str(uuid.UUID(bytes=payload)) if len(payload) == 16 else payload.hex()
                    print(f"Call {call_id}: UUID received")
                    continue

                if msg_type == MSG_DTMF:
                    print(f"Call {call_id}: DTMF {payload.decode(errors='replace')}")
                    continue

                if msg_type != MSG_SLIN8:
                    print(f"Call {call_id}: ignored message type 0x{msg_type:02x}")
                    continue

                block = pcm16_bytes_to_float32(payload)
                block_ms = int(len(block) / CALL_SAMPLE_RATE * 1000)
                level = rms(block)

                if not is_recording:
                    if level < self.args.start_threshold:
                        continue
                    is_recording = True
                    collected = [block]
                    silence_ms = 0
                    speech_ms = block_ms
                    print(f"Call {call_id}: recording")
                    continue

                collected.append(block)
                speech_ms += block_ms

                if level < self.args.stop_threshold:
                    silence_ms += block_ms
                else:
                    silence_ms = 0

                if silence_ms < self.args.silence_ms and speech_ms < int(self.args.max_seconds * 1000):
                    continue

                call_audio = np.concatenate(collected)
                is_recording = False
                collected = []
                silence_ms = 0
                speech_ms = 0

                duration = len(call_audio) / CALL_SAMPLE_RATE
                if duration < self.args.min_seconds:
                    print(f"Call {call_id}: ignored short audio {duration:.2f}s")
                    continue

                started = time.perf_counter()
                text = self.engine.transcribe(call_audio)
                elapsed = time.perf_counter() - started
                if not text:
                    print(f"Call {call_id}: no text recognized")
                    continue

                print(f"Call {call_id}: you said ({elapsed:.2f}s): {text}")
                reply_audio, reply_duration = self.engine.synthesize_for_call(text)
                print(f"Call {call_id}: replying ({reply_duration:.2f}s)")
                with send_lock:
                    send_queue.append(reply_audio)

        except EOFError:
            print(f"Call {call_id}: socket closed")
        finally:
            stop_sender.set()
            sender.join(timeout=1.0)

    def send_audio_loop(
        self,
        send_queue: deque[np.ndarray],
        send_lock: threading.Lock,
        stop_sender: threading.Event,
    ) -> None:
        packet_samples = max(1, int(CALL_SAMPLE_RATE * self.args.reply_packet_ms / 1000))
        keepalive_seconds = max(0.001, self.args.keepalive_ms / 1000)
        silence = np.zeros(packet_samples, dtype=np.float32)
        current: np.ndarray | None = None
        position = 0

        while not stop_sender.is_set():
            with send_lock:
                if current is None and send_queue:
                    current = send_queue.popleft()
                    position = 0

            if current is None:
                packet = silence
            else:
                packet = current[position : position + packet_samples]
                position += len(packet)
                if len(packet) < packet_samples:
                    packet = np.pad(packet, (0, packet_samples - len(packet)))
                    current = None
                    position = 0
                elif position >= len(current):
                    current = None
                    position = 0

            try:
                write_message(self.request, MSG_SLIN8, float32_to_pcm16_bytes(packet))
            except OSError:
                stop_sender.set()
                return

            time.sleep(keepalive_seconds)


class ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


def main() -> None:
    args = parse_args()
    engine = SpeechEngine(args)

    AudioSocketHandler.engine = engine
    AudioSocketHandler.args = args

    with ThreadingTCPServer((args.host, args.port), AudioSocketHandler) as server:
        print(f"AudioSocket server listening on {args.host}:{args.port}")
        server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
