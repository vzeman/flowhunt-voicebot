from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.provider_catalog import provider_catalog
from voicebot.providers import SUPPORTED_AGENT_PROVIDERS, SUPPORTED_STT_PROVIDERS, SUPPORTED_TTS_PROVIDERS
from voicebot.transcripts import TranscriptStore


class ProviderCatalogTests(unittest.TestCase):
    def test_provider_catalog_reports_all_supported_provider_sets(self) -> None:
        catalog = provider_catalog()

        self.assertEqual(set(catalog["stt"]["supported"]), SUPPORTED_STT_PROVIDERS)
        self.assertEqual(set(catalog["tts"]["supported"]), SUPPORTED_TTS_PROVIDERS)
        self.assertEqual(set(catalog["agent"]["supported"]), SUPPORTED_AGENT_PROVIDERS)
        self.assertIn("whisper", catalog["stt"]["native"])
        self.assertIn("supertonic", catalog["tts"]["native"])
        self.assertIn("openai-responses", catalog["agent"]["native"])

    def test_providers_endpoint_returns_provider_catalog(self) -> None:
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        client = TestClient(app)

        response = client.get("/providers")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), provider_catalog())


if __name__ == "__main__":
    unittest.main()
