from __future__ import annotations

import unittest

from voicebot.provider_config import (
    ProviderChoice,
    SecretReference,
    VoicebotProviderConfig,
    provider_selection_plan,
    validate_provider_config,
)
from voicebot.provider_catalog import _agent_capabilities, _stt_capabilities, _tts_capabilities


class ProviderConfigTests(unittest.TestCase):
    def descriptors(self):
        return {
            "stt": _stt_capabilities(),
            "tts": _tts_capabilities(),
            "agent": _agent_capabilities(),
        }

    def config(self) -> VoicebotProviderConfig:
        secret = SecretReference("openai-main", "workspace-1")
        return VoicebotProviderConfig(
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            stt=ProviderChoice("stt", "openai", model="gpt-4o-transcribe", secret_ref=secret, fallback_provider="whisper"),
            tts=ProviderChoice("tts", "openai", model="gpt-4o-mini-tts", secret_ref=secret, fallback_provider="supertonic"),
            agent=ProviderChoice("agent", "openai-responses", model="gpt-4.1", secret_ref=secret),
        )

    def test_valid_provider_config_has_no_validation_issues(self) -> None:
        issues = validate_provider_config(self.config(), self.descriptors())

        self.assertEqual(issues, [])

    def test_validation_reports_missing_secret_before_runtime(self) -> None:
        config = VoicebotProviderConfig(
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            stt=ProviderChoice("stt", "openai"),
            tts=ProviderChoice("tts", "supertonic"),
            agent=ProviderChoice("agent", "openai-responses"),
        )

        issues = validate_provider_config(config, self.descriptors())

        self.assertEqual(
            [(issue.family, issue.provider, issue.message) for issue in issues],
            [
                ("stt", "openai", "provider requires a secret reference"),
                ("agent", "openai-responses", "provider requires a secret reference"),
            ],
        )

    def test_validation_reports_unknown_provider_and_fallback(self) -> None:
        config = VoicebotProviderConfig(
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            stt=ProviderChoice("stt", "not-real", fallback_provider="also-bad"),
            tts=ProviderChoice("tts", "supertonic"),
            agent=ProviderChoice("agent", "openai-responses", secret_ref=SecretReference("openai", "workspace-1")),
        )

        issues = validate_provider_config(config, self.descriptors())

        self.assertIn(("stt", "not-real", "provider is not registered"), [(i.family, i.provider, i.message) for i in issues])

    def test_selection_plan_normalizes_providers_models_and_fallbacks(self) -> None:
        plan = provider_selection_plan(self.config())

        self.assertEqual(plan.workspace_id, "workspace-1")
        self.assertEqual(plan.providers["stt"], "openai")
        self.assertEqual(plan.fallbacks["stt"], "whisper")
        self.assertEqual(plan.models["agent"], "gpt-4.1")

    def test_secret_reference_requires_workspace_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "workspace_id"):
            SecretReference("openai", "")


if __name__ == "__main__":
    unittest.main()
