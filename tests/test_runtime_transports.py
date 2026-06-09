from __future__ import annotations

import unittest

import numpy as np

from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.main import build_transport_registry, transport_enabled
from voicebot.runtime_transports import AudioSocketServerTransport, WebRTCManagerTransport, build_runtime_transport_registry
from voicebot.workspace_model import VoicebotSessionStore


class FakeSTT:
    def transcribe(self, audio, sample_rate=8000):
        raise AssertionError("STT should not run in transport registry tests")

    def transcribe_stream(self, audio, sample_rate=8000):
        raise AssertionError("STT should not run in transport registry tests")


class FakeTTS:
    def synthesize(self, text: str):
        return np.zeros(80, dtype=np.float32), 0.01

    def synthesize_stream(self, text: str):
        yield self.synthesize(text)


class RuntimeTransportTests(unittest.TestCase):
    def test_build_transport_registry_uses_default_enabled_transports(self) -> None:
        registry = build_transport_registry(Settings())

        self.assertTrue(transport_enabled(registry, "asterisk_audiosocket"))
        self.assertTrue(transport_enabled(registry, "webrtc"))
        self.assertFalse(transport_enabled(registry, "twilio"))

    def test_build_transport_registry_can_enable_one_or_no_transports(self) -> None:
        webrtc_only = build_transport_registry(Settings(enabled_transports=("webrtc",)))
        none_enabled = build_transport_registry(Settings(enabled_transports=()))

        self.assertFalse(transport_enabled(webrtc_only, "asterisk_audiosocket"))
        self.assertTrue(transport_enabled(webrtc_only, "webrtc"))
        self.assertEqual(none_enabled.enabled(), ())

    def test_build_transport_registry_rejects_planned_or_unknown_transport_selection(self) -> None:
        with self.assertRaisesRegex(ValueError, "not implemented"):
            build_transport_registry(Settings(enabled_transports=("twilio",)))
        with self.assertRaisesRegex(KeyError, "not registered"):
            build_transport_registry(Settings(enabled_transports=("unknown",)))

    def test_runtime_transport_registry_registers_real_enabled_adapters(self) -> None:
        call_registry = CallRegistry()
        runtime_registry = build_runtime_transport_registry(
            Settings(enabled_transports=("webrtc",)),
            EventStore(max_context_events=20),
            call_registry,
            FakeSTT(),
            FakeTTS(),
            VoicebotSessionStore(),
        )

        self.assertFalse(transport_enabled(runtime_registry, "asterisk_audiosocket"))
        self.assertTrue(transport_enabled(runtime_registry, "webrtc"))
        self.assertIsInstance(runtime_registry.get("asterisk_audiosocket", require_enabled=False), AudioSocketServerTransport)
        self.assertIsInstance(runtime_registry.get("webrtc"), WebRTCManagerTransport)
        self.assertEqual([transport.kind for transport in runtime_registry.enabled()], ["webrtc"])

    def test_runtime_transport_registry_rejects_planned_selection(self) -> None:
        with self.assertRaisesRegex(ValueError, "not implemented"):
            build_runtime_transport_registry(
                Settings(enabled_transports=("twilio",)),
                EventStore(max_context_events=20),
                CallRegistry(),
                FakeSTT(),
                FakeTTS(),
                VoicebotSessionStore(),
            )

    def test_runtime_transport_registry_exposes_planned_adapter_contracts(self) -> None:
        runtime_registry = build_runtime_transport_registry(
            Settings(enabled_transports=("webrtc",)),
            EventStore(max_context_events=20),
            CallRegistry(),
            FakeSTT(),
            FakeTTS(),
            VoicebotSessionStore(),
        )

        payload = runtime_registry.to_dict()

        self.assertEqual(payload["transports"]["twilio"]["status"], "planned")
        self.assertEqual(payload["transports"]["twilio"]["adapter_contract"], "hosted_telephony_webhook")
        self.assertIn("planned", payload["transports"]["twilio"]["unavailable_reason"])

    def test_runtime_transport_registry_starts_and_stops_audiosocket_adapter(self) -> None:
        runtime_registry = build_runtime_transport_registry(
            Settings(enabled_transports=("asterisk_audiosocket",), audiosocket_host="127.0.0.1", audiosocket_port=0),
            EventStore(max_context_events=20),
            CallRegistry(),
            FakeSTT(),
            FakeTTS(),
            VoicebotSessionStore(),
        )

        started = runtime_registry.start_enabled()
        stopped = runtime_registry.shutdown_enabled()

        self.assertEqual(list(started), ["asterisk_audiosocket"])
        self.assertEqual(started["asterisk_audiosocket"]["status"], "ready")
        self.assertEqual(started["asterisk_audiosocket"]["details"]["host"], "127.0.0.1")
        self.assertEqual(stopped["asterisk_audiosocket"]["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
