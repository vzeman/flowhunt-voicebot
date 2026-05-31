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


if __name__ == "__main__":
    unittest.main()
