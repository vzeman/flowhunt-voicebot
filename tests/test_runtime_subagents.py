from __future__ import annotations

import unittest

from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.main import build_subagent_coordinator


class RuntimeSubagentTests(unittest.TestCase):
    def test_build_subagent_coordinator_registers_named_custom_provider_manifest(self) -> None:
        coordinator = build_subagent_coordinator(
            Settings(
                subagent_providers=(
                    {
                        "kind": "langgraph_agent",
                        "submit_url": "https://agent.example/langgraph/submit",
                        "poll_url": "https://agent.example/langgraph/poll",
                        "label": "LangGraph research agent",
                        "required_metadata": ["skill"],
                    },
                )
            ),
            EventStore(max_context_events=20),
        )

        provider = coordinator.provider_catalog()["providers"]["langgraph_agent"]

        self.assertTrue(provider["registered"])
        self.assertEqual(provider["label"], "LangGraph research agent")
        self.assertEqual(provider["required_metadata"], ["skill"])
        self.assertTrue(provider["supports_async_polling"])
        self.assertFalse(provider["supports_cancel"])

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

    def test_new_subagent_provider_manifests_take_precedence_over_legacy_http_setting(self) -> None:
        coordinator = build_subagent_coordinator(
            Settings(
                subagent_providers=(
                    {
                        "kind": "rasa_bot",
                        "submit_url": "https://agent.example/rasa/submit",
                        "label": "Rasa bot",
                    },
                ),
                http_subagent_providers=(
                    {
                        "submit_url": "https://agent.example/legacy/submit",
                        "label": "Legacy HTTP service",
                    },
                ),
            ),
            EventStore(max_context_events=20),
        )
        catalog = coordinator.provider_catalog()["providers"]

        self.assertTrue(catalog["rasa_bot"]["registered"])
        self.assertFalse(catalog["http_service"]["registered"])

    def test_build_subagent_coordinator_rejects_invalid_http_provider_manifest(self) -> None:
        with self.assertRaisesRegex(ValueError, "submit_url"):
            build_subagent_coordinator(
                Settings(http_subagent_providers=({"label": "Missing submit URL"},)),
                EventStore(max_context_events=20),
            )


if __name__ == "__main__":
    unittest.main()
