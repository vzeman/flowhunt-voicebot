from __future__ import annotations

import unittest

from voicebot.transports import (
    ASTERISK_AUDIOSOCKET_CAPABILITIES,
    WEBRTC_CAPABILITIES,
    CallControlRequest,
    CallRoute,
    StaticMediaTransport,
    transport_catalog,
)


class TransportContractTests(unittest.TestCase):
    def test_route_extracts_workspace_voicebot_and_preserves_extra_metadata(self) -> None:
        route = CallRoute.from_metadata(
            {
                "workspace_id": "workspace-1",
                "voicebot_id": "voicebot-1",
                "trunk_id": "trunk-1",
                "external_call_id": "provider-call-1",
                "customer_phone": "+421",
            }
        )

        self.assertEqual(route.workspace_id, "workspace-1")
        self.assertEqual(route.voicebot_id, "voicebot-1")
        self.assertEqual(route.trunk_id, "trunk-1")
        self.assertEqual(route.external_call_id, "provider-call-1")
        self.assertEqual(route.metadata, {"customer_phone": "+421"})

    def test_session_descriptor_contains_transport_route_and_capabilities(self) -> None:
        transport = StaticMediaTransport("webrtc", WEBRTC_CAPABILITIES, sample_rate=16000)

        descriptor = transport.describe_session(
            "call-1",
            {"workspace_id": "workspace-1", "voicebot_id": "voicebot-1"},
        )

        self.assertEqual(descriptor.call_id, "call-1")
        self.assertEqual(descriptor.transport, "webrtc")
        self.assertEqual(descriptor.sample_rate, 16000)
        self.assertEqual(descriptor.route.workspace_id, "workspace-1")
        self.assertTrue(descriptor.capabilities.supports("hangup"))
        self.assertFalse(descriptor.capabilities.supports("transfer"))

    def test_lifecycle_event_data_is_flat_and_event_friendly(self) -> None:
        transport = StaticMediaTransport("webrtc", WEBRTC_CAPABILITIES, sample_rate=16000)
        descriptor = transport.describe_session(
            "call-1",
            {"workspace_id": "workspace-1", "voicebot_id": "voicebot-1", "source": "browser"},
        )

        self.assertEqual(
            descriptor.lifecycle_event_data(),
            {
                "transport": "webrtc",
                "sample_rate": 16000,
                "workspace_id": "workspace-1",
                "voicebot_id": "voicebot-1",
                "metadata": {"source": "browser"},
            },
        )

    def test_supported_call_control_returns_successful_result(self) -> None:
        transport = StaticMediaTransport("asterisk_audiosocket", ASTERISK_AUDIOSOCKET_CAPABILITIES)

        result = transport.execute_call_control(
            CallControlRequest("call-1", "transfer", {"target": "support"})
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.action, "transfer")
        self.assertEqual(result.data["target"], "support")
        self.assertEqual(result.data["transport"], "asterisk_audiosocket")

    def test_unsupported_call_control_fails_cleanly(self) -> None:
        transport = StaticMediaTransport("webrtc", WEBRTC_CAPABILITIES)

        result = transport.execute_call_control(CallControlRequest("call-1", "transfer"))

        self.assertFalse(result.ok)
        self.assertEqual(result.action, "transfer")
        self.assertIn("not supported", result.reason or "")
        self.assertEqual(result.data, {"transport": "webrtc"})

    def test_transport_catalog_serializes_capabilities(self) -> None:
        catalog = transport_catalog()

        self.assertIn("asterisk_audiosocket", catalog["transports"])
        self.assertIn("webrtc", catalog["transports"])
        self.assertIn("transfer", catalog["transports"]["asterisk_audiosocket"]["capabilities"]["call_control"])
        self.assertNotIn("transfer", catalog["transports"]["webrtc"]["capabilities"]["call_control"])
        self.assertTrue(catalog["transports"]["webrtc"]["implemented"])


if __name__ == "__main__":
    unittest.main()
