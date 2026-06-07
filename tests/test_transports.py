from __future__ import annotations

import unittest

from voicebot.transports import (
    ASTERISK_AUDIOSOCKET_CAPABILITIES,
    WEBRTC_CAPABILITIES,
    CallControlRequest,
    CallRoute,
    MediaSessionDescriptor,
    StaticMediaTransport,
    TransportCapabilities,
    TransportDefinition,
    TransportRegistry,
    default_transport_registry,
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

    def test_session_descriptor_rejects_invalid_identity_and_sample_rate(self) -> None:
        with self.assertRaisesRegex(ValueError, "call_id"):
            MediaSessionDescriptor("", "webrtc")
        with self.assertRaisesRegex(ValueError, "sample_rate"):
            MediaSessionDescriptor("call-1", "webrtc", sample_rate=0)
        with self.assertRaisesRegex(ValueError, "sample_rate"):
            StaticMediaTransport("webrtc", WEBRTC_CAPABILITIES, sample_rate=0)
        with self.assertRaisesRegex(ValueError, "unsupported transport kind"):
            MediaSessionDescriptor("call-1", "unknown")
        with self.assertRaisesRegex(ValueError, "unsupported transport kind"):
            StaticMediaTransport("unknown", WEBRTC_CAPABILITIES)

    def test_transport_capabilities_reject_unknown_call_control_actions(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported call-control actions"):
            TransportCapabilities(call_control=frozenset({"hangup", "teleport"}))

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

    def test_descriptor_requires_workspace_scope_for_routed_sessions(self) -> None:
        routed = StaticMediaTransport("webrtc", WEBRTC_CAPABILITIES).describe_session(
            "call-1",
            {"workspace_id": "workspace-1", "voicebot_id": "voicebot-1"},
        )
        unrouted = StaticMediaTransport("webrtc", WEBRTC_CAPABILITIES).describe_session("call-2")

        self.assertEqual(routed.require_workspace_scope().workspace_id, "workspace-1")
        self.assertEqual(routed.require_workspace_scope().session_id, "call-1")
        with self.assertRaisesRegex(ValueError, "workspace_id"):
            unrouted.require_workspace_scope()

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

    def test_call_control_request_and_result_are_event_ready(self) -> None:
        request = CallControlRequest("call-1", "hangup", {"reason": "caller_requested"})
        result = StaticMediaTransport("webrtc", WEBRTC_CAPABILITIES).execute_call_control(request)

        self.assertEqual(
            request.as_event_data(),
            {"call_id": "call-1", "action": "hangup", "reason": "caller_requested"},
        )
        self.assertEqual(
            result.as_event_data(),
            {
                "call_id": "call-1",
                "action": "hangup",
                "ok": True,
                "transport": "webrtc",
                "reason": "caller_requested",
            },
        )

    def test_transport_catalog_serializes_capabilities(self) -> None:
        catalog = transport_catalog()

        self.assertIn("asterisk_audiosocket", catalog["transports"])
        self.assertIn("webrtc", catalog["transports"])
        self.assertIn("transfer", catalog["transports"]["asterisk_audiosocket"]["capabilities"]["call_control"])
        self.assertNotIn("transfer", catalog["transports"]["webrtc"]["capabilities"]["call_control"])
        self.assertTrue(catalog["transports"]["webrtc"]["implemented"])
        self.assertTrue(catalog["transports"]["webrtc"]["enabled"])
        self.assertFalse(catalog["transports"]["twilio"]["implemented"])

    def test_default_transport_registry_selects_enabled_implemented_transports(self) -> None:
        registry = default_transport_registry(enabled_kinds={"webrtc"})
        disabled_registry = default_transport_registry(enabled_kinds=set())

        selected = registry.get("webrtc")
        disabled = registry.get("asterisk_audiosocket", require_enabled=False)

        self.assertEqual(selected.kind, "webrtc")
        self.assertEqual(disabled.kind, "asterisk_audiosocket")
        self.assertEqual([transport.kind for transport in registry.enabled()], ["webrtc"])
        self.assertEqual(disabled_registry.enabled(), ())
        with self.assertRaisesRegex(ValueError, "not enabled"):
            registry.get("asterisk_audiosocket")
        with self.assertRaisesRegex(ValueError, "not implemented"):
            registry.get("twilio", require_enabled=False)

    def test_transport_registry_rejects_duplicate_and_invalid_definitions(self) -> None:
        registry = TransportRegistry()
        registry.register(StaticMediaTransport("webrtc", WEBRTC_CAPABILITIES))

        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(StaticMediaTransport("webrtc", WEBRTC_CAPABILITIES))
        with self.assertRaisesRegex(ValueError, "requires adapter"):
            TransportDefinition("webrtc", WEBRTC_CAPABILITIES, implemented=True)
        with self.assertRaisesRegex(ValueError, "must not provide adapter"):
            TransportDefinition(
                "twilio",
                TransportCapabilities(),
                implemented=False,
                transport=StaticMediaTransport("twilio", TransportCapabilities()),
            )


if __name__ == "__main__":
    unittest.main()
