from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.runtime_storage import (
    build_agent_task_tracker,
    build_event_store,
    build_subagent_task_store,
    build_transcript_store,
    default_storage_registry,
    selected_storage_drivers,
    storage_drivers_payload,
)
from voicebot.storage import attached_storage_driver, normalize_driver_name
from voicebot.transcripts import TranscriptStore


class StorageDriverTests(unittest.TestCase):
    def test_default_registry_lists_local_and_managed_drivers(self) -> None:
        registry = default_storage_registry()

        event_drivers = {definition.driver for definition in registry.definitions_for_family("events")}
        queue_drivers = {definition.driver for definition in registry.definitions_for_family("worker_queue")}

        self.assertIn("jsonl", event_drivers)
        self.assertIn("flowhunt_db", event_drivers)
        self.assertIn("redis_streams", queue_drivers)
        self.assertIn("flowhunt_queue", queue_drivers)

    def test_driver_aliases_are_normalized(self) -> None:
        self.assertEqual(normalize_driver_name("in-memory"), "memory")
        self.assertEqual(normalize_driver_name("fs"), "filesystem")

    def test_selected_storage_drivers_cover_all_required_families(self) -> None:
        selections = selected_storage_drivers(Settings())

        self.assertEqual(
            set(selections),
            {
                "events",
                "transcripts",
                "voicebot_sessions",
                "session_leases",
                "agent_tasks",
                "worker_queue",
                "worker_registry",
                "call_states",
                "provider_config",
                "sip_trunks",
                "subagent_tasks",
                "audio_artifacts",
            },
        )
        self.assertEqual(selections["events"].driver, "jsonl")
        self.assertEqual(selections["transcripts"].driver, "jsonl")
        self.assertEqual(selections["audio_artifacts"].driver, "filesystem")

    def test_local_store_builders_attach_driver_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                event_store_path=f"{directory}/events.jsonl",
                transcript_dir=f"{directory}/transcripts",
                agent_task_store_path=f"{directory}/agent_tasks.json",
                subagent_task_store_path=f"{directory}/subagent_tasks.json",
            )
            transcripts = build_transcript_store(settings)
            events = build_event_store(settings, transcripts)
            agent_tasks = build_agent_task_tracker(settings)
            subagent_tasks = build_subagent_task_store(settings)

        self.assertEqual(attached_storage_driver(transcripts).family, "transcripts")
        self.assertEqual(attached_storage_driver(events).driver, "jsonl")
        self.assertEqual(attached_storage_driver(agent_tasks).family, "agent_tasks")
        self.assertEqual(attached_storage_driver(subagent_tasks).family, "subagent_tasks")

    def test_jsonl_alias_still_selects_json_for_object_stores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tracker = build_agent_task_tracker(
                Settings(agent_task_store_provider="jsonl", agent_task_store_path=f"{directory}/agent_tasks.json")
            )

        self.assertEqual(attached_storage_driver(tracker).driver, "json")
        self.assertEqual(attached_storage_driver(tracker).configured_driver, "jsonl")

    def test_unknown_storage_driver_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported storage driver"):
            storage_drivers_payload(Settings(event_store_provider="unknown"))

    def test_storage_drivers_endpoint_exposes_selected_drivers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                EventStore(max_context_events=20),
                CallRegistry(),
                AgentTaskTracker(),
                WebSocketHub(),
                TranscriptStore(directory),
                None,
            )
            response = TestClient(app).get("/storage/drivers")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("events", payload["registry"]["families"])
        self.assertEqual(payload["selected"]["events"]["driver"], "jsonl")
        self.assertEqual(payload["selected"]["provider_config"]["driver"], "memory")


if __name__ == "__main__":
    unittest.main()
