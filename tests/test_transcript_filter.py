from __future__ import annotations

import unittest

from voicebot.transcript_filter import should_drop_agent_transcript


class TranscriptFilterTests(unittest.TestCase):
    def test_drops_long_hallucinated_transcript_from_tiny_audio(self) -> None:
        decision = should_drop_agent_transcript(
            "Dobre, rezervovali ste si termin na pondelok.",
            stale=False,
            min_chars=2,
            min_tokens=1,
            audio_duration_seconds=0.24,
        )

        self.assertTrue(decision.should_drop)
        self.assertEqual(decision.reason, "low_signal_transcript")

    def test_keeps_normal_duration_transcript(self) -> None:
        decision = should_drop_agent_transcript(
            "What's the pricing of LiveAgent?",
            stale=False,
            min_chars=2,
            min_tokens=1,
            audio_duration_seconds=1.2,
            audio_rms=0.05,
        )

        self.assertFalse(decision.should_drop)

    def test_drops_prompt_biased_transcript_from_low_level_audio(self) -> None:
        decision = should_drop_agent_transcript(
            "status page",
            stale=False,
            min_chars=2,
            min_tokens=1,
            audio_duration_seconds=2.3,
            audio_rms=0.008,
        )

        self.assertTrue(decision.should_drop)
        self.assertEqual(decision.reason, "low_signal_transcript")

    def test_keeps_single_word_intent_at_noise_boundary(self) -> None:
        decision = should_drop_agent_transcript(
            "Downtime",
            stale=False,
            min_chars=5,
            min_tokens=2,
            audio_duration_seconds=0.68,
            audio_rms=0.024,
        )

        self.assertFalse(decision.should_drop)

    def test_drops_very_short_single_word_noise(self) -> None:
        decision = should_drop_agent_transcript(
            "Hi",
            stale=False,
            min_chars=5,
            min_tokens=2,
            audio_duration_seconds=0.68,
            audio_rms=0.024,
        )

        self.assertTrue(decision.should_drop)
        self.assertEqual(decision.reason, "low_signal_transcript")


if __name__ == "__main__":
    unittest.main()
