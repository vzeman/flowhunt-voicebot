from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.realtime_quality import latency_budget_defaults, metric_latency_budget_seconds, realtime_audio_profile, realtime_audio_profile_issues
from voicebot.scaling import latency_metric_signals
from voicebot.transcripts import TranscriptStore


class RealtimeQualityTests(unittest.TestCase):
    def test_realtime_audio_profile_exposes_cancellation_streaming_jitter_and_cache(self) -> None:
        profile = realtime_audio_profile(Settings())

        self.assertEqual(realtime_audio_profile_issues(profile), [])
        self.assertTrue(profile["cancellation"]["barge_in_interrupts_playback"])
        self.assertTrue(profile["streaming"]["tts_streaming_chunks_supported"])
        self.assertEqual(profile["streaming"]["tts_stream_min_chunk_seconds"], 0.04)
        self.assertEqual(profile["streaming"]["tts_stream_max_chunk_seconds"], 0.30)
        self.assertEqual(profile["latency_budgets"]["agent_seconds"], 1.2)
        self.assertTrue(profile["normalization"]["webrtc_jitter_buffer_enabled"])
        self.assertTrue(profile["cache"]["tts_cache_enabled"])

    def test_realtime_audio_profile_reports_invalid_quality_settings(self) -> None:
        profile = realtime_audio_profile(
            Settings(
                start_threshold=0.01,
                stop_threshold=0.02,
                tts_chunk_chars=0,
                tts_stream_min_chunk_seconds=0.5,
                tts_stream_max_chunk_seconds=0.1,
                tts_cache_enabled=False,
            )
        )

        issues = realtime_audio_profile_issues(profile)

        self.assertIn({"issue": "stop_threshold exceeds start_threshold"}, issues)
        self.assertIn({"issue": "tts_chunk_chars must be positive"}, issues)
        self.assertIn({"issue": "tts_stream_max_chunk_seconds must be at least min chunk seconds"}, issues)
        self.assertIn({"issue": "tts cache is disabled"}, issues)

    def test_latency_budget_defaults_and_metric_mapping_are_configurable(self) -> None:
        settings = Settings(latency_budget_agent_seconds=0.5, latency_budget_tts_first_audio_seconds=0.25)

        self.assertEqual(latency_budget_defaults(settings)["agent_seconds"], 0.5)
        self.assertEqual(metric_latency_budget_seconds(settings, "agent_response_latency_seconds"), 0.5)
        self.assertEqual(metric_latency_budget_seconds(settings, "tts_first_audio_latency_seconds"), 0.25)

    def test_latency_metric_signals_include_first_audio_metrics(self) -> None:
        events = EventStore(max_context_events=20)
        events.append("call-1", "metrics", {"name": "tts_first_audio_latency_seconds", "value": 0.12})
        events.append("call-1", "metrics", {"name": "tts_stream_first_audio_latency_seconds", "value": 0.08})
        events.append("call-1", "metrics", {"name": "end_of_speech_to_playback_started_seconds", "value": 1.4})

        signals = latency_metric_signals(events.list_events())

        self.assertEqual(signals["tts_first_audio_latency_seconds"]["avg"], 0.12)
        self.assertEqual(signals["tts_stream_first_audio_latency_seconds"]["avg"], 0.08)
        self.assertEqual(signals["end_of_speech_to_playback_started_seconds"]["avg"], 1.4)

    def test_realtime_audio_profile_endpoint_returns_profile_and_issues(self) -> None:
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        client = TestClient(app)

        response = client.get("/realtime/audio-profile")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["issues"], [])
        self.assertIn("turn_detection", response.json()["profile"])


if __name__ == "__main__":
    unittest.main()
