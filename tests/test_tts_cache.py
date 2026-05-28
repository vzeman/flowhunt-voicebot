from __future__ import annotations

import tempfile
import unittest

import numpy as np

from voicebot.tts import CachedTTSProvider, TTSCacheConfig


class CountingTTS:
    def __init__(self) -> None:
        self.calls = 0

    def synthesize(self, text: str):
        self.calls += 1
        return np.ones(80, dtype=np.float32) * self.calls, 0.01


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


if __name__ == "__main__":
    unittest.main()
