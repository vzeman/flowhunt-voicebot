from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.provider_catalog import provider_catalog
from voicebot.providers import (
    SUPPORTED_AGENT_PROVIDERS,
    SUPPORTED_STT_PROVIDERS,
    SUPPORTED_TTS_PROVIDERS,
    ProviderCapabilities,
    ProviderDescriptor,
)
from voicebot.transcripts import TranscriptStore


class ProviderCatalogTests(unittest.TestCase):
    def test_provider_catalog_reports_all_supported_provider_sets(self) -> None:
        catalog = provider_catalog()

        self.assertEqual(set(catalog["stt"]["supported"]), SUPPORTED_STT_PROVIDERS)
        self.assertEqual(set(catalog["tts"]["supported"]), SUPPORTED_TTS_PROVIDERS)
        self.assertEqual(set(catalog["agent"]["supported"]), SUPPORTED_AGENT_PROVIDERS)
        self.assertIn("whisper", catalog["stt"]["native"])
        self.assertIn("deepgram", catalog["stt"]["native"])
        self.assertIn("assemblyai", catalog["stt"]["native"])
        self.assertIn("supertonic", catalog["tts"]["native"])
        self.assertIn("openai-responses", catalog["agent"]["native"])
        self.assertTrue(catalog["stt"]["capabilities"]["openai"]["capabilities"]["interruption_support"])
        self.assertEqual(
            catalog["tts"]["capabilities"]["supertonic"]["capabilities"]["output_audio_format"],
            "pcm_f32_8000",
        )
        self.assertTrue(catalog["agent"]["capabilities"]["openai-responses"]["capabilities"]["native_tools"])

    def test_registered_provider_descriptors_are_valid(self) -> None:
        catalog = provider_catalog()
        issues = []
        for family in ("stt", "tts", "agent"):
            for descriptor_data in catalog[family]["capabilities"].values():
                descriptor = ProviderDescriptor(
                    provider=descriptor_data["provider"],
                    family=descriptor_data["family"],
                    adapter=descriptor_data["adapter"],
                    capabilities=ProviderCapabilities(
                        modalities=frozenset(descriptor_data["capabilities"]["modalities"]),
                        streaming=descriptor_data["capabilities"]["streaming"],
                        languages=tuple(descriptor_data["capabilities"]["languages"]),
                        required_credentials=tuple(descriptor_data["capabilities"]["required_credentials"]),
                        latency_profile=descriptor_data["capabilities"]["latency_profile"],
                        interruption_support=descriptor_data["capabilities"]["interruption_support"],
                        output_audio_format=descriptor_data["capabilities"]["output_audio_format"],
                        usage_metadata=tuple(descriptor_data["capabilities"]["usage_metadata"]),
                        native_tools=descriptor_data["capabilities"]["native_tools"],
                    ),
                    models=tuple(descriptor_data["models"]),
                    config=descriptor_data["config"],
                )
                for issue in descriptor.validation_issues():
                    issues.append((descriptor.provider, issue))

        self.assertEqual(issues, [])

    def test_provider_descriptor_validation_reports_malformed_descriptor(self) -> None:
        descriptor = ProviderDescriptor(
            provider="OpenAI",
            family="stt",
            adapter="native",
            capabilities=ProviderCapabilities(
                modalities=frozenset({"tts"}),
                required_credentials=("",),
                latency_profile="slow",
                languages=("",),
                usage_metadata=("",),
                output_audio_format=" ",
            ),
            models=("",),
        )

        self.assertEqual(
            descriptor.validation_issues(),
            (
                "provider must be normalized",
                "capabilities modalities must match descriptor family",
                "required credentials must not be blank",
                "latency profile must be supported",
                "languages must not be blank",
                "usage metadata keys must not be blank",
                "output audio format must not be blank",
                "models must not be blank",
            ),
        )

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
