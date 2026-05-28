from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.multimodal import (
    ModalityCapabilities,
    MultimodalContent,
    MultimodalContext,
    MultimodalContextStore,
    validate_multimodal_content,
)
from voicebot.providers import ProviderCapabilities
from voicebot.transcripts import TranscriptStore
from voicebot.transports import WEBRTC_CAPABILITIES


class MultimodalTests(unittest.TestCase):
    def test_content_part_serializes_for_agent_context(self) -> None:
        part = MultimodalContent(
            modality="image",
            direction="input",
            mime_type="image/png",
            uri="s3://bucket/image.png",
            metadata={"source": "webrtc"},
        )

        self.assertEqual(
            part.to_agent_part(),
            {
                "modality": "image",
                "direction": "input",
                "mime_type": "image/png",
                "uri": "s3://bucket/image.png",
                "metadata": {"source": "webrtc"},
            },
        )

    def test_context_packages_multiple_modalities_without_session_changes(self) -> None:
        context = MultimodalContext("call-1", workspace_id="workspace-1", voicebot_id="voicebot-1")
        context = context.add(MultimodalContent("chat", "input", text="hello"))
        context = context.add(MultimodalContent("visual_card", "output", metadata={"card": "summary"}))

        payload = context.to_agent_context()

        self.assertEqual(payload["workspace_id"], "workspace-1")
        self.assertEqual([part["modality"] for part in payload["parts"]], ["chat", "visual_card"])

    def test_modality_capabilities_can_declare_browser_visual_support(self) -> None:
        capabilities = ModalityCapabilities(
            input=frozenset({"audio", "chat", "image"}),
            output=frozenset({"audio", "chat", "visual_card"}),
        )

        self.assertTrue(capabilities.supports_input("image"))
        self.assertTrue(capabilities.supports_output("visual_card"))
        self.assertFalse(capabilities.supports_output("avatar_video"))

    def test_transport_capabilities_have_audio_text_default_modalities(self) -> None:
        self.assertTrue(WEBRTC_CAPABILITIES.modalities.supports_input("audio"))
        self.assertTrue(WEBRTC_CAPABILITIES.modalities.supports_output("text"))

    def test_provider_capabilities_accept_future_multimodal_agent_modalities(self) -> None:
        capabilities = ProviderCapabilities(
            modalities=frozenset({"agent", "image_input", "visual_output"}),
            native_tools=True,
        )

        self.assertTrue(capabilities.supports("image_input"))
        self.assertTrue(capabilities.supports("visual_output"))

    def test_context_store_accumulates_parts_by_call(self) -> None:
        store = MultimodalContextStore()

        store.add_part("call-1", MultimodalContent("chat", "input", text="hello"), workspace_id="workspace-1")
        context = store.add_part("call-1", MultimodalContent("visual_card", "output", text="card"))

        self.assertEqual(context.workspace_id, "workspace-1")
        self.assertEqual([part.modality for part in context.parts], ["chat", "visual_card"])

    def test_context_store_deletes_call_context_for_cleanup(self) -> None:
        store = MultimodalContextStore()
        store.add_part("call-1", MultimodalContent("chat", "input", text="hello"), workspace_id="workspace-1")

        self.assertTrue(store.delete("call-1"))
        self.assertFalse(store.delete("call-1"))
        self.assertEqual(store.get("call-1").parts, ())

    def test_context_store_rejects_cross_scope_parts_for_same_call(self) -> None:
        store = MultimodalContextStore()
        store.add_part(
            "call-1",
            MultimodalContent("chat", "input", text="hello"),
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            session_id="session-1",
        )

        with self.assertRaisesRegex(ValueError, "workspace_id"):
            store.add_part("call-1", MultimodalContent("chat", "input", text="other"), workspace_id="workspace-2")
        with self.assertRaisesRegex(ValueError, "voicebot_id"):
            store.add_part("call-1", MultimodalContent("chat", "input", text="other"), voicebot_id="voicebot-2")
        with self.assertRaisesRegex(ValueError, "session_id"):
            store.add_part("call-1", MultimodalContent("chat", "input", text="other"), session_id="session-2")

    def test_multimodal_validation_checks_capabilities_and_content_shape(self) -> None:
        capabilities = ModalityCapabilities(input=frozenset({"audio"}), output=frozenset({"audio", "text"}))

        issues = validate_multimodal_content(MultimodalContent("image", "input", uri="s3://image.png"), capabilities)
        empty = validate_multimodal_content(MultimodalContent("text", "output"), capabilities)

        self.assertEqual(issues[0].field, "modality")
        self.assertIn("not supported", issues[0].message)
        self.assertEqual(empty[0].field, "content")
        self.assertEqual(validate_multimodal_content(MultimodalContent("text", "output", text="hello"), capabilities), ())

    def test_multimodal_api_adds_part_and_emits_event(self) -> None:
        events = EventStore(max_context_events=20)
        client = self.build_client(events)

        response = client.post(
            "/calls/call-1/multimodal/parts",
            json={
                "modality": "image",
                "direction": "input",
                "mime_type": "image/png",
                "uri": "s3://workspace/file.png",
                "workspace_id": "workspace-1",
                "metadata": {"source": "browser"},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["context"]["parts"][0]["modality"], "image")
        self.assertEqual(events.list_events(call_id="call-1")[-1].type, "multimodal_content_added")

        context_response = client.get("/calls/call-1/multimodal")
        self.assertEqual(context_response.json()["parts"][0]["uri"], "s3://workspace/file.png")

    def test_multimodal_api_rejects_unknown_modality(self) -> None:
        client = self.build_client(EventStore(max_context_events=20))

        response = client.post(
            "/calls/call-1/multimodal/parts",
            json={"modality": "unknown", "direction": "input"},
        )

        self.assertEqual(response.status_code, 400)

    def test_multimodal_api_rejects_empty_content_part(self) -> None:
        client = self.build_client(EventStore(max_context_events=20))

        response = client.post(
            "/calls/call-1/multimodal/parts",
            json={"modality": "text", "direction": "input"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"][0]["field"], "content")

    def test_multimodal_api_rejects_cross_workspace_part_for_same_call(self) -> None:
        client = self.build_client(EventStore(max_context_events=20))
        first = {
            "modality": "text",
            "direction": "input",
            "text": "hello",
            "workspace_id": "workspace-1",
        }
        second = {
            **first,
            "text": "different workspace",
            "workspace_id": "workspace-2",
        }

        self.assertEqual(client.post("/calls/call-1/multimodal/parts", json=first).status_code, 200)
        response = client.post("/calls/call-1/multimodal/parts", json=second)

        self.assertEqual(response.status_code, 400)
        self.assertIn("workspace_id", response.json()["detail"])

    def build_client(self, events: EventStore) -> TestClient:
        app = create_app(
            events,
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        return TestClient(app)


if __name__ == "__main__":
    unittest.main()
