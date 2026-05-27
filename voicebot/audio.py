from __future__ import annotations

import socket
import struct

import numpy as np
from scipy.signal import resample_poly


MSG_TERMINATE = 0x00
MSG_UUID = 0x01
MSG_DTMF = 0x03
MSG_SLIN8 = 0x10

CALL_SAMPLE_RATE = 8_000
STT_SAMPLE_RATE = 16_000


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


def read_audiosocket_message(sock: socket.socket) -> tuple[int, bytes]:
    header = recv_exact(sock, 3)
    msg_type = header[0]
    payload_len = struct.unpack("!H", header[1:3])[0]
    payload = recv_exact(sock, payload_len) if payload_len else b""
    return msg_type, payload


def write_audiosocket_message(sock: socket.socket, msg_type: int, payload: bytes = b"") -> None:
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
