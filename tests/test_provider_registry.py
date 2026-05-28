from __future__ import annotations

import unittest

from voicebot.config import Settings
from voicebot.provider_registry import ProviderRegistry, default_provider_registry
from voicebot.providers import ProviderCapabilities, ProviderDescriptor
from voicebot.transports import CallRoute


class ProviderRegistryTests(unittest.TestCase):
    def test_provider_registry_builds_registered_stt_provider(self) -> None:
        registry = ProviderRegistry()
        registry.register_stt("custom_stt", lambda settings: {"provider": settings.stt_provider})

        provider = registry.build_stt(Settings(stt_provider="custom_stt"))

        self.assertEqual(provider, {"provider": "custom_stt"})

    def test_provider_registry_builds_registered_tts_provider(self) -> None:
        registry = ProviderRegistry()
        registry.register_tts("custom_tts", lambda settings: {"provider": settings.tts_provider})

        provider = registry.build_tts(Settings(tts_provider="custom_tts"))

        self.assertEqual(provider, {"provider": "custom_tts"})

    def test_provider_registry_reports_known_but_unimplemented_stt_provider(self) -> None:
        registry = ProviderRegistry()

        with self.assertRaisesRegex(ValueError, "Unsupported STT provider adapter for 'deepgram'"):
            registry.build_stt(Settings(stt_provider="deepgram"))

    def test_provider_registry_reports_unknown_tts_provider(self) -> None:
        registry = ProviderRegistry()

        with self.assertRaisesRegex(ValueError, "Unknown TTS provider: not-real"):
            registry.build_tts(Settings(tts_provider="not-real"))

    def test_default_provider_registry_registers_current_runtime_adapters(self) -> None:
        registry = default_provider_registry()

        self.assertIn("whisper", registry.stt_factories)
        self.assertIn("openai", registry.stt_factories)
        self.assertIn("supertonic", registry.tts_factories)
        self.assertIn("openai", registry.tts_factories)

    def test_registry_tracks_provider_capabilities(self) -> None:
        registry = default_provider_registry()

        stt = registry.describe_stt("openai")
        tts = registry.describe_tts("supertonic")

        self.assertIsNotNone(stt)
        self.assertEqual(stt.provider, "openai")
        self.assertTrue(stt.capabilities.supports("stt"))
        self.assertEqual(stt.capabilities.latency_profile, "interactive")
        self.assertIsNotNone(tts)
        self.assertEqual(tts.capabilities.output_audio_format, "pcm_f32_8000")

    def test_registry_rejects_invalid_provider_descriptor(self) -> None:
        registry = ProviderRegistry()
        descriptor = ProviderDescriptor(
            provider="custom-stt",
            family="stt",
            adapter="native",
            capabilities=ProviderCapabilities(modalities=frozenset()),
        )

        with self.assertRaisesRegex(ValueError, "Invalid provider descriptor"):
            registry.register_stt("custom_stt", lambda settings: None, descriptor)

    def test_registry_rejects_descriptor_for_different_provider(self) -> None:
        registry = ProviderRegistry()
        descriptor = ProviderDescriptor(
            provider="other-stt",
            family="stt",
            adapter="native",
            capabilities=ProviderCapabilities(modalities=frozenset({"stt"})),
        )

        with self.assertRaisesRegex(ValueError, "descriptor provider must match"):
            registry.register_stt("custom_stt", lambda settings: None, descriptor)

    def test_registry_resolves_provider_by_workspace_and_voicebot_route(self) -> None:
        registry = ProviderRegistry()
        registry.register_stt("default_stt", lambda settings: {"provider": settings.stt_provider})
        registry.register_stt("workspace_stt", lambda settings: {"provider": "workspace"})
        registry.register_stt("voicebot_stt", lambda settings: {"provider": "voicebot"})
        registry.route_stt("workspace-1", None, "workspace_stt")
        registry.route_stt("workspace-1", "voicebot-1", "voicebot_stt")

        workspace_route = CallRoute(workspace_id="workspace-1", voicebot_id="other")
        voicebot_route = CallRoute(workspace_id="workspace-1", voicebot_id="voicebot-1")

        self.assertEqual(registry.resolve_stt_provider(Settings(stt_provider="default_stt"), workspace_route), "workspace-stt")
        self.assertEqual(registry.resolve_stt_provider(Settings(stt_provider="default_stt"), voicebot_route), "voicebot-stt")

    def test_registry_rejects_route_to_unregistered_stt_adapter(self) -> None:
        registry = ProviderRegistry()

        with self.assertRaisesRegex(ValueError, "no adapter is registered"):
            registry.route_stt("workspace-1", "voicebot-1", "deepgram")

    def test_registry_rejects_route_to_unregistered_tts_adapter(self) -> None:
        registry = ProviderRegistry()

        with self.assertRaisesRegex(ValueError, "no adapter is registered"):
            registry.route_tts("workspace-1", "voicebot-1", "elevenlabs")


if __name__ == "__main__":
    unittest.main()
