from __future__ import annotations

import unittest
from dataclasses import replace

import numpy as np

from voicebot.config import Settings
from voicebot.realtime_audio import (
    AudioChunkNormalizer,
    DebugAudioCapture,
    TurnDetectionConfig,
    TurnDetector,
    turn_detection_config_from_settings,
)


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

        result = detector.process_block(np.full(100, 0.1, dtype=np.float32), playback_active=True)

        self.assertEqual(result.decision, "ignored")

    def test_turn_detector_ignores_playback_echo_below_barge_in_threshold(self) -> None:
        detector = TurnDetector(config())

        result = detector.process_block(np.full(100, 0.3, dtype=np.float32), playback_active=True)

        self.assertEqual(result.decision, "ignored")

    def test_turn_detector_allows_barge_in_above_barge_in_threshold(self) -> None:
        detector = TurnDetector(config())

        result = detector.process_block(np.full(100, 0.6, dtype=np.float32), playback_active=True)

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

    def test_audio_chunk_normalizer_rejects_invalid_transport_settings(self) -> None:
        invalid_settings = [
            {"source_rate": 0, "target_rate": 16000, "channels": 1},
            {"source_rate": 48000, "target_rate": 0, "channels": 1},
            {"source_rate": 48000, "target_rate": 16000, "channels": 0},
        ]

        for settings in invalid_settings:
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    AudioChunkNormalizer(**settings)

    def test_turn_detection_config_can_be_built_from_runtime_settings(self) -> None:
        settings = Settings(
            start_threshold=0.12,
            stop_threshold=0.04,
            vad_start_ms=80,
            silence_ms=500,
            min_seconds=0.3,
            max_seconds=12.0,
            barge_in_threshold=0.7,
        )

        resolved = turn_detection_config_from_settings(settings, sample_rate=16000)

        self.assertEqual(resolved.sample_rate, 16000)
        self.assertEqual(resolved.start_threshold, 0.12)
        self.assertEqual(resolved.stop_threshold, 0.04)
        self.assertEqual(resolved.vad_start_ms, 80)
        self.assertEqual(resolved.silence_ms, 500)
        self.assertEqual(resolved.min_seconds, 0.3)
        self.assertEqual(resolved.max_seconds, 12.0)
        self.assertEqual(resolved.barge_in_threshold, 0.7)

    def test_turn_detection_config_rejects_invalid_values(self) -> None:
        invalid_configs = [
            {"sample_rate": 0},
            {"start_threshold": -0.1},
            {"stop_threshold": -0.1},
            {"stop_threshold": 0.3},
            {"vad_start_ms": -1},
            {"silence_ms": 0},
            {"min_seconds": -0.1},
            {"max_seconds": 0},
            {"min_seconds": 2.0, "max_seconds": 1.0},
            {"barge_in_threshold": 0.1},
        ]

        for overrides in invalid_configs:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    replace(config(), **overrides)

    def test_debug_audio_capture_is_gated_and_bounded(self) -> None:
        disabled = DebugAudioCapture(enabled=False, sample_rate=1000, max_seconds=1.0)
        disabled.append(np.ones(100, dtype=np.float32))

        enabled = DebugAudioCapture(enabled=True, sample_rate=1000, max_seconds=0.15)
        enabled.append(np.ones(100, dtype=np.float32))
        enabled.append(np.ones(100, dtype=np.float32) * 0.5)

        self.assertEqual(disabled.summary()["samples"], 0)
        self.assertEqual(enabled.summary()["samples"], 100)
        self.assertAlmostEqual(float(enabled.audio().mean()), 0.5)
        enabled.clear()
        self.assertEqual(enabled.summary()["duration_seconds"], 0.0)

    def test_debug_audio_capture_rejects_invalid_settings(self) -> None:
        with self.assertRaisesRegex(ValueError, "sample_rate"):
            DebugAudioCapture(enabled=True, sample_rate=0)
        with self.assertRaisesRegex(ValueError, "max_seconds"):
            DebugAudioCapture(enabled=True, sample_rate=1000, max_seconds=-1)


if __name__ == "__main__":
    unittest.main()
