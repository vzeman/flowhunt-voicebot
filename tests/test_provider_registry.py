from __future__ import annotations

import unittest

from voicebot.config import Settings
from voicebot.provider_registry import ProviderRegistry, default_provider_registry


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


if __name__ == "__main__":
    unittest.main()
