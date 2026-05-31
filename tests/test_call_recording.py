from __future__ import annotations

import tempfile
import unittest

import numpy as np

from voicebot.call_recording import SpeechOnlyCallRecorder, recording_artifact_id
from voicebot.storage.artifacts import FilesystemArtifactStore


class SpeechOnlyCallRecordingTests(unittest.TestCase):
    def test_recorder_writes_only_voiced_segments_with_original_timing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FilesystemArtifactStore(directory)
            recorder = SpeechOnlyCallRecorder("call-1", store, silence_threshold=0.01)

            recorder.append_speech("caller", np.array([0.0, 0.0, 0.2, 0.2, 0.0], dtype=np.float32), 100)
            recorder.append_speech("voicebot", np.zeros(20, dtype=np.float32), 100)
            metadata = recorder.finalize()

            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(metadata["call_id"], "call-1")
            self.assertEqual(metadata["segment_count"], 1)
            self.assertTrue(metadata["silence_removed"])
            self.assertEqual(metadata["segments"][0]["source"], "caller")
            self.assertEqual(metadata["segments"][0]["samples"], 2)
            self.assertEqual(metadata["segments"][0]["playback_start_seconds"], 0.0)
            self.assertIsNotNone(store.get(recording_artifact_id("call-1")))

    def test_recorder_resamples_segments_to_single_playback_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FilesystemArtifactStore(directory)
            recorder = SpeechOnlyCallRecorder("call-2", store, silence_threshold=0.01)

            recorder.append_speech("caller", np.ones(160, dtype=np.float32) * 0.2, 16000)
            recorder.append_speech("voicebot", np.ones(80, dtype=np.float32) * 0.2, 8000)
            metadata = recorder.finalize()

            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(metadata["sample_rate"], 16000)
            self.assertEqual(metadata["segment_count"], 2)
            self.assertEqual(metadata["segments"][1]["playback_sample_rate"], 16000)
            self.assertGreater(metadata["segments"][1]["playback_samples"], metadata["segments"][1]["samples"])

    def test_recorder_coalesces_adjacent_playback_packets_with_same_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FilesystemArtifactStore(directory)
            recorder = SpeechOnlyCallRecorder("call-3", store, silence_threshold=0.01)
            started = recorder._started_at
            packet = np.ones(160, dtype=np.float32) * 0.2
            metadata = {"response_to_event_id": 6659, "response_kind": "colleague_result"}

            recorder.append_speech("voicebot", packet, 8000, end_monotonic=started + 34.299, metadata=metadata)
            recorder.append_speech("voicebot", packet, 8000, end_monotonic=started + 34.326, metadata=metadata)
            recorder.append_speech("voicebot", packet, 8000, end_monotonic=started + 34.350, metadata=metadata)
            saved = recorder.finalize()

            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved["segment_count"], 1)
            self.assertEqual(saved["segments"][0]["source"], "voicebot")
            self.assertEqual(saved["segments"][0]["samples"], 480)
            self.assertEqual(saved["segments"][0]["duration_seconds"], 0.06)
            self.assertEqual(saved["segments"][0]["metadata"], metadata)

    def test_recorder_keeps_segments_separate_across_sources_or_large_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FilesystemArtifactStore(directory)
            recorder = SpeechOnlyCallRecorder("call-4", store, silence_threshold=0.01)
            started = recorder._started_at
            packet = np.ones(160, dtype=np.float32) * 0.2

            recorder.append_speech("voicebot", packet, 8000, end_monotonic=started + 1.02, metadata={"id": 1})
            recorder.append_speech("caller", packet, 8000, end_monotonic=started + 1.05, metadata={"id": 1})
            recorder.append_speech("voicebot", packet, 8000, end_monotonic=started + 2.02, metadata={"id": 1})
            saved = recorder.finalize()

            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved["segment_count"], 3)


if __name__ == "__main__":
    unittest.main()
