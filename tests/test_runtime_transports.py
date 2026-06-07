from __future__ import annotations

import unittest

from voicebot.config import Settings
from voicebot.main import build_transport_registry, transport_enabled


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


if __name__ == "__main__":
    unittest.main()
