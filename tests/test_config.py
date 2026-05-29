from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from voicebot.config import Settings, env_json_list, redacted_settings


class ConfigTests(unittest.TestCase):
    def test_env_json_list_returns_default_when_env_is_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            value = env_json_list("VOICEBOT_TEST_PIPELINE", [{"name": "stt"}])

        self.assertEqual(value, ({"name": "stt"},))

    def test_env_json_list_parses_json_list(self) -> None:
        with patch.dict(os.environ, {"VOICEBOT_TEST_PIPELINE": '[{"name":"fan-out","options":{"branches":[]}}]'}):
            value = env_json_list("VOICEBOT_TEST_PIPELINE", [])

        self.assertEqual(value, ({"name": "fan-out", "options": {"branches": []}},))

    def test_env_json_list_rejects_non_list_values(self) -> None:
        with patch.dict(os.environ, {"VOICEBOT_TEST_PIPELINE": '{"name":"stt"}'}):
            with self.assertRaisesRegex(ValueError, "VOICEBOT_TEST_PIPELINE must be a JSON list"):
                env_json_list("VOICEBOT_TEST_PIPELINE", [])

    def test_redacted_settings_hides_sensitive_values(self) -> None:
        settings = Settings(
            openai_api_key="configured-openai",
            ami_password="configured-ami",
            stt_pipeline=({"name": "stt"},),
        )

        result = redacted_settings(settings)

        self.assertEqual(result["openai_api_key"], {"configured": True, "redacted": True})
        self.assertEqual(result["ami_password"], {"configured": True, "redacted": True})
        self.assertEqual(result["stt_pipeline"], [{"name": "stt"}])
        self.assertEqual(result["stt_provider"], settings.stt_provider)
        self.assertEqual(result["agent_task_responded_event_retention"], 10000)
        self.assertEqual(result["call_state_store_provider"], "json")
        self.assertEqual(result["call_state_store_path"], "/data/call_states.json")
        self.assertEqual(result["allowed_workspace_ids"], [])

    def test_agent_task_retention_can_be_configured(self) -> None:
        settings = Settings(agent_task_responded_event_retention=25)

        result = redacted_settings(settings)

        self.assertEqual(settings.agent_task_responded_event_retention, 25)
        self.assertEqual(result["agent_task_responded_event_retention"], 25)

    def test_voice_defaults_prefer_openai_stt_quality(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings()

        self.assertEqual(settings.language, "en")
        self.assertEqual(settings.start_threshold, 0.020)
        self.assertEqual(settings.stop_threshold, 0.010)
        self.assertEqual(settings.silence_ms, 700)
        self.assertEqual(settings.min_seconds, 0.8)
        self.assertEqual(settings.max_reply_chars, 240)
        self.assertEqual(settings.deferred_response_wait_seconds, 30.0)
        self.assertEqual(settings.debug_audio_dir, "/data/debug-audio")
        self.assertTrue(settings.tts_cache_enabled)
        self.assertEqual(settings.tts_cache_dir, "/data/tts-cache")

    def test_debug_audio_capture_can_be_configured(self) -> None:
        settings = Settings(debug_audio_capture=False, debug_audio_dir="/tmp/debug-audio")

        self.assertFalse(settings.debug_audio_capture)
        self.assertEqual(settings.debug_audio_dir, "/tmp/debug-audio")


if __name__ == "__main__":
    unittest.main()
