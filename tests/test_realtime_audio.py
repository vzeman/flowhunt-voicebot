from __future__ import annotations

import unittest
from dataclasses import replace

import numpy as np

from voicebot.realtime_audio import AudioChunkNormalizer, TurnDetectionConfig, TurnDetector


def config() -> TurnDetectionConfig:
    return TurnDetectionConfig(
        sample_rate=1000,
        start_threshold=0.2,
        stop_threshold=0.1,
        vad_start_ms=100,
        silence_ms=200,
        min_seconds=0.2,
        max_seconds=2.0,
        barge_in_threshold=0.5,
    )


class RealtimeAudioTests(unittest.TestCase):
    def test_turn_detector_requires_configured_start_duration(self) -> None:
        detector = TurnDetector(config())

        first = detector.process_block(np.full(50, 0.3, dtype=np.float32))
        second = detector.process_block(np.full(50, 0.3, dtype=np.float32))

        self.assertEqual(first.decision, "pending_start")
        self.assertEqual(second.decision, "speech_started")
        self.assertTrue(second.started)

    def test_turn_detector_finishes_after_silence(self) -> None:
        detector = TurnDetector(config())
        detector.process_block(np.full(100, 0.3, dtype=np.float32))
        detector.process_block(np.full(100, 0.3, dtype=np.float32))

        first_silence = detector.process_block(np.zeros(100, dtype=np.float32))
        second_silence = detector.process_block(np.zeros(100, dtype=np.float32))

        self.assertEqual(first_silence.decision, "speech_continues")
        self.assertEqual(second_silence.decision, "speech_finished")
        self.assertTrue(second_silence.finished)
        self.assertGreaterEqual(second_silence.duration, 0.2)
        self.assertIsNotNone(second_silence.audio)

    def test_turn_detector_ignores_low_playback_echo(self) -> None:
        detector = TurnDetector(config())

        result = detector.process_block(np.full(100, 0.3, dtype=np.float32), playback_active=True)

        self.assertEqual(result.decision, "ignored")

    def test_turn_detector_allows_barge_in_above_threshold(self) -> None:
        detector = TurnDetector(config())

        result = detector.process_block(np.full(100, 0.8, dtype=np.float32), playback_active=True)

        self.assertEqual(result.decision, "speech_started")
        self.assertTrue(result.interrupt_playback)

    def test_turn_detector_marks_short_speech(self) -> None:
        short_config = replace(config(), min_seconds=0.5)
        detector = TurnDetector(short_config)
        detector.process_block(np.full(100, 0.3, dtype=np.float32))

        result = detector.process_block(np.zeros(200, dtype=np.float32))

        self.assertEqual(result.decision, "speech_too_short")
        self.assertTrue(result.finished)

    def test_turn_detection_result_exposes_metric_data(self) -> None:
        detector = TurnDetector(config())

        result = detector.process_block(np.full(100, 0.8, dtype=np.float32), playback_active=True)
        data = result.metric_data(session_id="session-1", turn_id=1)

        self.assertEqual(data["decision"], "speech_started")
        self.assertEqual(data["block_ms"], 100)
        self.assertTrue(data["started"])
        self.assertFalse(data["finished"])
        self.assertTrue(data["interrupt_playback"])
        self.assertEqual(data["duration"], 0.0)
        self.assertEqual(data["session_id"], "session-1")
        self.assertEqual(data["turn_id"], 1)
        self.assertAlmostEqual(data["level"], 0.8, places=6)

    def test_audio_chunk_normalizer_downmixes_and_resamples(self) -> None:
        stereo = np.ones((2, 480), dtype=np.int16) * 8192
        normalizer = AudioChunkNormalizer(source_rate=48000, target_rate=16000, channels=2)

        audio = normalizer.normalize(stereo)

        self.assertEqual(len(audio), 160)
        self.assertAlmostEqual(float(audio.mean()), 0.25, delta=0.02)

    def test_audio_chunk_normalizer_scales_float_audio_outside_unit_range(self) -> None:
        samples = np.ones(80, dtype=np.float32) * 8192.0
        normalizer = AudioChunkNormalizer(source_rate=8000, target_rate=8000)

        audio = normalizer.normalize(samples)

        self.assertAlmostEqual(float(audio.mean()), 0.25, delta=0.02)


if __name__ == "__main__":
    unittest.main()
