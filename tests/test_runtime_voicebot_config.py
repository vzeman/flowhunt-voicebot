from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.provider_config import ProviderChoice, SecretReference, VoicebotProviderConfig
from voicebot.runtime_config import (
    VoicebotPromptConfigStore,
    VoicebotPromptConfig,
    VoicebotQuotaConfig,
    VoicebotRealtimeConfig,
    VoicebotRuntimeConfig,
    VoicebotRuntimeConfigStore,
)
from voicebot.transcripts import TranscriptStore
from voicebot.workspace_model import VoicebotDefinition, VoicebotStore


class RuntimeVoicebotConfigTests(unittest.TestCase):
    def provider_config(self) -> VoicebotProviderConfig:
        secret = SecretReference("openai-main", "workspace-1")
        return VoicebotProviderConfig(
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            stt=ProviderChoice("stt", "openai", model="gpt-4o-transcribe", secret_ref=secret),
            tts=ProviderChoice("tts", "openai", model="gpt-4o-mini-tts", secret_ref=secret),
            agent=ProviderChoice("agent", "openai-responses", model="gpt-4.1-mini", secret_ref=secret),
        )

    def build_client(self) -> tuple[TestClient, EventStore]:
        self.directory = tempfile.TemporaryDirectory()
        events = EventStore(max_context_events=50)
        voicebots = VoicebotStore()
        voicebots.create(VoicebotDefinition("workspace-1", "voicebot-1"))
        app = create_app(
            events,
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(self.directory.name),
            None,
            voicebots=voicebots,
            prompt_configs=VoicebotPromptConfigStore(),
        )
        return TestClient(app), events

    def tearDown(self) -> None:
        directory = getattr(self, "directory", None)
        if directory is not None:
            directory.cleanup()

    def test_runtime_config_rejects_scope_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider config scope"):
            VoicebotRuntimeConfig(
                workspace_id="workspace-1",
                voicebot_id="voicebot-2",
                config_version=1,
                providers=self.provider_config(),
            )

    def test_runtime_config_store_increments_versions_on_activation(self) -> None:
        store = VoicebotRuntimeConfigStore()
        first = VoicebotRuntimeConfig(
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            config_version=1,
            providers=self.provider_config(),
            prompts=VoicebotPromptConfig(language="en"),
            realtime=VoicebotRealtimeConfig(tts_chunk_chars=80),
            quotas=VoicebotQuotaConfig(max_concurrent_sessions=2),
        )

        saved_first = store.save(first)
        saved_second = store.save(first)

        self.assertEqual(saved_first.config_version, 1)
        self.assertEqual(saved_second.config_version, 2)

    def test_runtime_config_endpoint_validates_saves_redacts_and_emits_version_event(self) -> None:
        client, events = self.build_client()

        response = client.put(
            "/workspaces/workspace-1/voicebots/voicebot-1/runtime-config",
            json={
                "providers": {
                    "stt": {
                        "provider": "openai",
                        "model": "gpt-4o-transcribe",
                        "secret_ref": {"name": "openai-main"},
                    },
                    "tts": {
                        "provider": "openai",
                        "model": "gpt-4o-mini-tts",
                        "secret_ref": {"name": "openai-main"},
                    },
                    "agent": {
                        "provider": "openai-responses",
                        "model": "gpt-4.1-mini",
                        "secret_ref": {"name": "openai-main"},
                    },
                },
                "prompts": {
                    "greeting": "Say hello.",
                    "colleague_progress_message": "I asked a specialist.",
                    "system_prompt": "Be concise.",
                    "stt_prompt": "LiveAgent FlowHunt",
                    "language": "en",
                },
                "realtime": {"silence_ms": 420, "tts_chunk_chars": 75},
                "quotas": {"max_concurrent_sessions": 3, "enabled_actions": ["say", "hangup_call"]},
                "subagents": {
                    "complex_backend": "flow",
                    "flowhunt_workspace_id": "workspace-1",
                    "flowhunt_flow_id": "flow-1",
                    "prompts": {
                        "flowhunt_flow": {
                            "before_call_prompt": "I will ask the specialist now.",
                            "after_call_prompt": "The specialist is working on it.",
                            "result_prompt": "Summarize this colleague result: {result}",
                        }
                    },
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["config"]["config_version"], 1)
        self.assertEqual(payload["config"]["prompts"]["greeting"], "Say hello.")
        self.assertEqual(payload["config"]["prompts"]["colleague_progress_message"], "I asked a specialist.")
        self.assertEqual(
            payload["config"]["subagents"]["prompts"]["flowhunt_flow"]["result_prompt"],
            "Summarize this colleague result: {result}",
        )
        self.assertEqual(payload["config"]["realtime"]["tts_chunk_chars"], 75)
        self.assertEqual(payload["config"]["quotas"]["max_concurrent_sessions"], 3)
        self.assertEqual(payload["config"]["providers"]["stt"]["secret_ref"], {"name": "openai-main", "workspace_id": "workspace-1"})
        self.assertNotIn("api_key", str(payload).lower())
        event = events.list_events(call_id="system")[-1]
        self.assertEqual(event.type, "runtime_config_updated")
        self.assertEqual(event.data["config_version"], 1)

        get_response = client.get("/workspaces/workspace-1/voicebots/voicebot-1/runtime-config")

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json()["config"]["config_version"], 1)

    def test_runtime_config_endpoint_validates_provider_before_activation(self) -> None:
        client, events = self.build_client()

        response = client.put(
            "/workspaces/workspace-1/voicebots/voicebot-1/runtime-config",
            json={
                "providers": {
                    "stt": {"provider": "openai"},
                    "tts": {"provider": "supertonic"},
                    "agent": {"provider": "openai-responses"},
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(events.list_events(call_id="system"), [])

    def test_prompt_config_endpoint_sets_and_patches_voicebot_prompts(self) -> None:
        client, events = self.build_client()

        put_response = client.put(
            "/workspaces/workspace-1/voicebots/voicebot-1/prompts",
            json={
                "greeting": "Pozdrav volajuceho po slovensky.",
                "filler_message": "Chvíľku strpenia.",
                "colleague_progress_message": "Pýtam sa kolegu a hneď vám poviem výsledok.",
                "system_prompt": "Use concise Slovak.",
                "stt_prompt": "LiveAgent FlowHunt",
                "language": "sk",
            },
        )

        self.assertEqual(put_response.status_code, 200)
        self.assertEqual(put_response.json()["prompts"]["language"], "sk")
        self.assertEqual(put_response.json()["prompts"]["filler_message"], "Chvíľku strpenia.")
        self.assertEqual(
            put_response.json()["prompts"]["colleague_progress_message"],
            "Pýtam sa kolegu a hneď vám poviem výsledok.",
        )

        patch_response = client.patch(
            "/workspaces/workspace-1/voicebots/voicebot-1/prompts",
            json={
                "system_prompt": "Use friendly Slovak.",
                "filler_message": "Hneď to overím.",
                "colleague_progress_message": "Kolega to preveruje.",
            },
        )

        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.json()["prompts"]["greeting"], "Pozdrav volajuceho po slovensky.")
        self.assertEqual(patch_response.json()["prompts"]["filler_message"], "Hneď to overím.")
        self.assertEqual(patch_response.json()["prompts"]["colleague_progress_message"], "Kolega to preveruje.")
        self.assertEqual(patch_response.json()["prompts"]["system_prompt"], "Use friendly Slovak.")
        get_response = client.get("/workspaces/workspace-1/voicebots/voicebot-1/prompts")
        self.assertEqual(get_response.json()["source"], "prompt_override")
        self.assertEqual(events.list_events(call_id="system")[-1].type, "voicebot_prompts_updated")


if __name__ == "__main__":
    unittest.main()
