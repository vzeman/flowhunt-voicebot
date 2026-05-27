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


if __name__ == "__main__":
    unittest.main()
