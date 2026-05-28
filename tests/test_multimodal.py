from __future__ import annotations

import unittest

from voicebot.multimodal import ModalityCapabilities, MultimodalContent, MultimodalContext
from voicebot.providers import ProviderCapabilities
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


if __name__ == "__main__":
    unittest.main()
