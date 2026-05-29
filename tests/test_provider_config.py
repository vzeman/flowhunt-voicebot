from __future__ import annotations

import unittest
from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.provider_config import (
    ProviderChoice,
    ProviderConfigStore,
    SecretReference,
    VoicebotProviderConfig,
    provider_selection_plan,
    validate_provider_config,
)
from voicebot.provider_catalog import _agent_capabilities, _stt_capabilities, _tts_capabilities
from voicebot.transcripts import TranscriptStore


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

    def test_validation_reports_cross_workspace_secret_reference(self) -> None:
        config = VoicebotProviderConfig(
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            stt=ProviderChoice("stt", "openai", secret_ref=SecretReference("openai", "workspace-2")),
            tts=ProviderChoice("tts", "supertonic"),
            agent=ProviderChoice("agent", "openai-responses", secret_ref=SecretReference("openai", "workspace-1")),
        )

        issues = validate_provider_config(config, self.descriptors())

        self.assertIn(
            ("stt", "openai", "secret reference belongs to a different workspace"),
            [(issue.family, issue.provider, issue.message) for issue in issues],
        )

    def test_validation_reports_fallback_missing_required_secret(self) -> None:
        config = VoicebotProviderConfig(
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            stt=ProviderChoice("stt", "whisper", fallback_provider="openai"),
            tts=ProviderChoice("tts", "supertonic"),
            agent=ProviderChoice("agent", "anthropic", secret_ref=SecretReference("anthropic", "workspace-1")),
        )

        issues = validate_provider_config(config, self.descriptors())

        self.assertIn(
            ("stt", "openai", "fallback provider requires a secret reference"),
            [(issue.family, issue.provider, issue.message) for issue in issues],
        )

    def test_validation_reports_family_mismatch_and_same_fallback(self) -> None:
        config = VoicebotProviderConfig(
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            stt=ProviderChoice("tts", "whisper", fallback_provider="whisper"),
            tts=ProviderChoice("tts", "supertonic"),
            agent=ProviderChoice("agent", "anthropic", secret_ref=SecretReference("anthropic", "workspace-1")),
        )

        issues = validate_provider_config(config, self.descriptors())

        self.assertIn(
            ("stt", "whisper", "choice family must be stt"),
            [(issue.family, issue.provider, issue.message) for issue in issues],
        )
        self.assertIn(
            ("stt", "whisper", "fallback provider must be different from provider"),
            [(issue.family, issue.provider, issue.message) for issue in issues],
        )

    def test_validation_reports_known_provider_model_mismatch(self) -> None:
        config = VoicebotProviderConfig(
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            stt=ProviderChoice("stt", "whisper", model="not-a-whisper-model"),
            tts=ProviderChoice("tts", "supertonic", model="supertonic-3"),
            agent=ProviderChoice("agent", "anthropic", model="custom-model", secret_ref=SecretReference("anthropic", "workspace-1")),
        )

        issues = validate_provider_config(config, self.descriptors())

        self.assertIn(
            ("stt", "whisper", "model is not supported by provider: not-a-whisper-model"),
            [(issue.family, issue.provider, issue.message) for issue in issues],
        )
        self.assertNotIn(
            ("agent", "anthropic", "model is not supported by provider: custom-model"),
            [(issue.family, issue.provider, issue.message) for issue in issues],
        )

    def test_selection_plan_normalizes_providers_models_and_fallbacks(self) -> None:
        plan = provider_selection_plan(self.config())

        self.assertEqual(plan.workspace_id, "workspace-1")
        self.assertEqual(plan.providers["stt"], "openai")
        self.assertEqual(plan.fallbacks["stt"], "whisper")
        self.assertEqual(plan.models["agent"], "gpt-4.1")

    def test_secret_reference_requires_workspace_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "name"):
            SecretReference(" ", "workspace-1")
        with self.assertRaisesRegex(ValueError, "workspace_id"):
            SecretReference("openai", " ")

    def test_provider_choice_rejects_invalid_identity_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported provider family"):
            ProviderChoice("voice", "openai")
        with self.assertRaisesRegex(ValueError, "provider is required"):
            ProviderChoice("stt", "")
        with self.assertRaisesRegex(ValueError, "model"):
            ProviderChoice("stt", "openai", model=" ")
        with self.assertRaisesRegex(ValueError, "fallback_provider"):
            ProviderChoice("stt", "openai", fallback_provider=" ")

    def test_provider_config_requires_workspace_and_voicebot_ids(self) -> None:
        valid = self.config()
        with self.assertRaisesRegex(ValueError, "workspace_id"):
            VoicebotProviderConfig(" ", valid.voicebot_id, valid.stt, valid.tts, valid.agent)
        with self.assertRaisesRegex(ValueError, "voicebot_id"):
            VoicebotProviderConfig(valid.workspace_id, " ", valid.stt, valid.tts, valid.agent)

    def test_provider_config_store_saves_by_workspace_voicebot(self) -> None:
        store = ProviderConfigStore()

        store.save(self.config())

        self.assertEqual(store.get("workspace-1", "voicebot-1"), self.config())
        self.assertEqual([config.voicebot_id for config in store.list(workspace_id="workspace-1")], ["voicebot-1"])

    def test_provider_config_api_validates_saves_and_reads_config(self) -> None:
        client = self.build_client()

        response = client.put(
            "/workspaces/workspace-1/voicebots/voicebot-1/providers",
            json={
                "stt": {
                    "provider": "openai",
                    "model": "gpt-4o-transcribe",
                    "secret_ref": {"name": "openai-main"},
                    "fallback_provider": "whisper",
                },
                "tts": {
                    "provider": "openai",
                    "model": "gpt-4o-mini-tts",
                    "secret_ref": {"name": "openai-main"},
                    "fallback_provider": "supertonic",
                },
                "agent": {
                    "provider": "openai-responses",
                    "model": "gpt-4.1",
                    "secret_ref": {"name": "openai-main"},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["selection_plan"]["providers"]["agent"], "openai-responses")

        read_response = client.get("/workspaces/workspace-1/voicebots/voicebot-1/providers")
        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(read_response.json()["config"]["stt"]["secret_ref"]["workspace_id"], "workspace-1")

    def test_provider_config_api_returns_validation_errors_without_saving(self) -> None:
        client = self.build_client()

        response = client.put(
            "/workspaces/workspace-1/voicebots/voicebot-1/providers",
            json={
                "stt": {"provider": "openai"},
                "tts": {"provider": "supertonic"},
                "agent": {"provider": "openai-responses"},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(
            [(issue["family"], issue["provider"]) for issue in response.json()["validation"]],
            [("stt", "openai"), ("agent", "openai-responses")],
        )
        self.assertEqual(client.get("/workspaces/workspace-1/voicebots/voicebot-1/providers").status_code, 404)

    def build_client(self) -> TestClient:
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        return TestClient(app)


if __name__ == "__main__":
    unittest.main()
