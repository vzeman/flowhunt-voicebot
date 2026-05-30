from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from voicebot.tts import CALL_SAMPLE_RATE, OPENAI_TTS_PCM_SAMPLE_RATE, CachedTTSProvider, OpenAITTSProvider, TTSCacheConfig, pcm16le_bytes_to_float32


class CountingTTS:
    def __init__(self) -> None:
        self.calls = 0

    def synthesize(self, text: str):
        self.calls += 1
        return np.ones(80, dtype=np.float32) * self.calls, 0.01


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeStreamingResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_bytes(self, chunk_size: int):
        _ = chunk_size
        yield from self.chunks


class _FakeSpeech:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.requests = []
        self.with_streaming_response = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return _FakeStreamingResponse(self.chunks)


class TTSCacheTests(unittest.TestCase):
    def test_cached_tts_reuses_audio_for_same_text_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inner = CountingTTS()
            provider = CachedTTSProvider(
                inner,
                directory,
                TTSCacheConfig(provider="openai", model="tts-model", voice="alloy", language="en"),
            )

            first_audio, first_duration = provider.synthesize("Hello")
            second_audio, second_duration = provider.synthesize("Hello")

        self.assertEqual(inner.calls, 1)
        self.assertEqual(first_duration, second_duration)
        np.testing.assert_array_equal(first_audio, second_audio)

    def test_cached_tts_key_includes_voice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_inner = CountingTTS()
            first_provider = CachedTTSProvider(
                first_inner,
                directory,
                TTSCacheConfig(provider="openai", model="tts-model", voice="alloy", language="en"),
            )
            second_inner = CountingTTS()
            second_provider = CachedTTSProvider(
                second_inner,
                directory,
                TTSCacheConfig(provider="openai", model="tts-model", voice="verse", language="en"),
            )

            first_provider.synthesize("Hello")
            second_provider.synthesize("Hello")

        self.assertEqual(first_inner.calls, 1)
        self.assertEqual(second_inner.calls, 1)

    def test_cached_tts_auto_language_partitions_cache_by_detected_language(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inner = CountingTTS()
            provider = CachedTTSProvider(
                inner,
                directory,
                TTSCacheConfig(provider="openai", model="tts-model", voice="alloy", language="auto"),
            )

            provider.synthesize("Dobrý deň, rozprávate po slovensky?")
            provider.synthesize("Hello, please check the status.")
            provider.synthesize("Dobrý deň, rozprávate po slovensky?")

        self.assertEqual(inner.calls, 2)

    def test_cached_tts_writes_artifact_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inner = CountingTTS()
            provider = CachedTTSProvider(
                inner,
                directory,
                TTSCacheConfig(provider="openai", model="tts-model", voice="alloy", language="en"),
            )

            provider.synthesize("Hello")

            metadata_files = list(Path(directory).glob("*.metadata.json"))
            metadata = metadata_files[0].read_text(encoding="utf-8")

        self.assertEqual(len(metadata_files), 1)
        self.assertIn('"kind": "tts_cache"', metadata)

    def test_pcm16le_bytes_to_float32_ignores_trailing_odd_byte(self) -> None:
        audio = pcm16le_bytes_to_float32(b"\x00\x40\xff")

        self.assertEqual(audio.shape, (1,))
        self.assertAlmostEqual(float(audio[0]), 0.5, places=4)

    def test_openai_tts_streams_pcm_chunks_as_call_audio(self) -> None:
        provider = OpenAITTSProvider.__new__(OpenAITTSProvider)
        provider._model = "gpt-4o-mini-tts"
        provider._voice = "alloy"
        provider._lock = _NoopLock()
        samples = (np.ones(OPENAI_TTS_PCM_SAMPLE_RATE // 10, dtype=np.int16) * 8192).astype("<i2")
        speech = _FakeSpeech([samples.tobytes()[:101], samples.tobytes()[101:]])
        provider._client = type("Client", (), {"audio": type("Audio", (), {"speech": speech})()})()

        chunks = list(provider.synthesize_stream("Hello"))

        self.assertGreaterEqual(len(chunks), 1)
        audio = np.concatenate([chunk for chunk, _duration in chunks])
        duration = sum(duration for _chunk, duration in chunks)
        self.assertGreater(audio.size, 0)
        self.assertAlmostEqual(duration, 0.1, places=2)
        self.assertEqual(speech.requests[0]["response_format"], "pcm")
        self.assertEqual(speech.requests[0]["model"], "gpt-4o-mini-tts")
        self.assertEqual(speech.requests[0]["voice"], "alloy")
        self.assertAlmostEqual(len(audio), CALL_SAMPLE_RATE // 10, delta=2)


if __name__ == "__main__":
    unittest.main()
