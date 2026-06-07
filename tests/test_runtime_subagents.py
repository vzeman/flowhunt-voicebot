from __future__ import annotations

import unittest

from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.main import build_subagent_coordinator


class RuntimeSubagentTests(unittest.TestCase):
    def test_build_subagent_coordinator_registers_configured_http_provider(self) -> None:
        coordinator = build_subagent_coordinator(
            Settings(
                http_subagent_providers=(
                    {
                        "submit_url": "https://agent.example/submit",
                        "poll_url": "https://agent.example/poll",
                        "cancel_url": "https://agent.example/cancel",
                        "label": "Research HTTP service",
                        "headers": {"Authorization": "Bearer token"},
                        "required_metadata": ["skill"],
                        "timeout_seconds": 5,
                    },
                )
            ),
            EventStore(max_context_events=20),
        )

        provider = coordinator.provider_catalog()["providers"]["http_service"]

        self.assertTrue(provider["registered"])
        self.assertEqual(provider["label"], "Research HTTP service")
        self.assertEqual(provider["required_metadata"], ["skill"])
        self.assertTrue(provider["supports_async_polling"])
        self.assertTrue(provider["supports_cancel"])

    def test_build_subagent_coordinator_rejects_invalid_http_provider_manifest(self) -> None:
        with self.assertRaisesRegex(ValueError, "submit_url"):
            build_subagent_coordinator(
                Settings(http_subagent_providers=({"label": "Missing submit URL"},)),
                EventStore(max_context_events=20),
            )


if __name__ == "__main__":
    unittest.main()
