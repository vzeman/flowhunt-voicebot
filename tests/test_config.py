from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from voicebot.config import env_json_list


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


if __name__ == "__main__":
    unittest.main()
