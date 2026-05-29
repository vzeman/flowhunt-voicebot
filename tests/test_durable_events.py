from __future__ import annotations

import json
import tempfile
import unittest

from voicebot.config import Settings
from voicebot.events import EventStore, JsonEventStore, event_from_dict
from voicebot.agent_tasks import AgentTaskTracker, JsonAgentTaskTracker
from voicebot.runtime_storage import build_agent_task_tracker, build_event_store, build_voicebot_session_store
from voicebot.transcripts import TranscriptStore
from voicebot.workspace_model import JsonVoicebotSessionStore, VoicebotSessionRecord, VoicebotSessionStore


class DurableEventTests(unittest.TestCase):
    def test_json_event_store_reloads_events_and_preserves_next_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/events.jsonl"
            first_store = JsonEventStore(path, max_context_events=100)
            first = first_store.append("call-1", "call_started", {"workspace_id": "workspace-1"})
            second = first_store.append("call-1", "call_connected", {"workspace_id": "workspace-1"})

            reloaded = JsonEventStore(path, max_context_events=100)
            third = reloaded.append("call-1", "call_ended", {"workspace_id": "workspace-1"})

            events = reloaded.list_events()

        self.assertEqual([event.id for event in events], [first.id, second.id, third.id])
        self.assertEqual(third.id, second.id + 1)

    def test_json_event_store_reports_load_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/events.jsonl"
            valid_event = {
                "id": 1,
                "call_id": "call-1",
                "type": "call_started",
                "timestamp": "2026-05-28T00:00:00+00:00",
                "data": {"workspace_id": "workspace-1"},
            }
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(valid_event) + "\n")
                handle.write("\n")
                handle.write("{bad json}\n")
                handle.write(json.dumps({"id": "bad"}) + "\n")

            store = JsonEventStore(path, max_context_events=100)

        self.assertEqual(store.load_diagnostics["loaded_events"], 1)
        self.assertEqual(store.load_diagnostics["skipped_blank_lines"], 1)
        self.assertEqual(store.load_diagnostics["skipped_malformed_json"], 1)
        self.assertEqual(store.load_diagnostics["skipped_invalid_events"], 1)
        self.assertEqual(store.load_diagnostics["skipped_duplicate_event_ids"], 0)

    def test_json_event_store_skips_duplicate_event_ids_on_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/events.jsonl"
            first = {
                "id": 1,
                "call_id": "call-1",
                "type": "call_started",
                "timestamp": "2026-05-28T00:00:00+00:00",
                "data": {"source": "first"},
            }
            duplicate = {
                **first,
                "call_id": "call-2",
                "timestamp": "2026-05-28T00:00:01+00:00",
                "data": {"source": "duplicate"},
            }
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(first) + "\n")
                handle.write(json.dumps(duplicate) + "\n")

            store = JsonEventStore(path, max_context_events=100)
            next_event = store.append("call-3", "call_connected", {})

        self.assertEqual([event.call_id for event in store.list_events()], ["call-1", "call-3"])
        self.assertEqual(store.load_diagnostics["loaded_events"], 1)
        self.assertEqual(store.load_diagnostics["skipped_duplicate_event_ids"], 1)
        self.assertEqual(next_event.id, 2)

    def test_event_store_filters_by_workspace_voicebot_and_session(self) -> None:
        store = EventStore(max_context_events=100)
        store.append(
            "call-1",
            "call_started",
            {"workspace_id": "workspace-1", "voicebot_id": "voicebot-1", "session_id": "session-1"},
        )
        store.append(
            "call-2",
            "call_started",
            {"workspace_id": "workspace-1", "voicebot_id": "voicebot-2", "session_id": "session-2"},
        )
        store.append(
            "call-3",
            "call_started",
            {"workspace_id": "workspace-2", "voicebot_id": "voicebot-1", "session_id": "session-3"},
        )

        scoped = store.list_events(workspace_id="workspace-1", voicebot_id="voicebot-1", session_id="session-1")

        self.assertEqual([event.call_id for event in scoped], ["call-1"])

    def test_event_store_can_filter_session_by_call_id_when_session_id_missing(self) -> None:
        store = EventStore(max_context_events=100)
        store.append("call-1", "call_started", {"workspace_id": "workspace-1"})
        store.append("call-2", "call_started", {"workspace_id": "workspace-1"})

        scoped = store.list_events(workspace_id="workspace-1", session_id="call-2")

        self.assertEqual([event.call_id for event in scoped], ["call-2"])

    def test_event_from_dict_rejects_invalid_payloads(self) -> None:
        self.assertIsNone(event_from_dict({"id": "bad"}))
        self.assertIsNone(
            event_from_dict(
                {
                    "id": 0,
                    "call_id": "call-1",
                    "type": "call_started",
                    "timestamp": "2026-05-28T00:00:00+00:00",
                    "data": {},
                }
            )
        )
        self.assertIsNone(
            event_from_dict(
                {
                    "id": 1,
                    "call_id": " ",
                    "type": "call_started",
                    "timestamp": "2026-05-28T00:00:00+00:00",
                    "data": {},
                }
            )
        )
        self.assertIsNone(
            event_from_dict(
                {
                    "id": 1,
                    "call_id": "call-1",
                    "type": "",
                    "timestamp": "2026-05-28T00:00:00+00:00",
                    "data": {},
                }
            )
        )
        self.assertIsNotNone(
            event_from_dict(
                {
                    "id": 1,
                    "call_id": "call-1",
                    "type": "call_started",
                    "timestamp": "2026-05-28T00:00:00+00:00",
                    "data": {},
                }
            )
        )

    def test_runtime_builder_selects_json_event_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                event_store_provider="json",
                event_store_path=f"{directory}/events.jsonl",
                transcript_dir=f"{directory}/transcripts",
            )
            transcripts = TranscriptStore(settings.transcript_dir)

            store = build_event_store(settings, transcripts)
            store.append("call-1", "call_started", {"workspace_id": "workspace-1"})
            reloaded = build_event_store(settings, transcripts)

        self.assertIsInstance(store, JsonEventStore)
        self.assertEqual([event.type for event in reloaded.list_events()], ["call_started"])

    def test_runtime_builder_can_select_memory_event_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                event_store_provider="memory",
                event_store_path=f"{directory}/events.jsonl",
                transcript_dir=f"{directory}/transcripts",
            )

            store = build_event_store(settings, TranscriptStore(settings.transcript_dir))

        self.assertIsInstance(store, EventStore)
        self.assertNotIsInstance(store, JsonEventStore)

    def test_runtime_builder_selects_json_voicebot_session_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                voicebot_session_store_provider="json",
                voicebot_session_store_path=f"{directory}/sessions.json",
            )

            store = build_voicebot_session_store(settings)
            store.save(VoicebotSessionRecord("session-1", "workspace-1", "voicebot-1"))
            reloaded = build_voicebot_session_store(settings)

        self.assertIsInstance(store, JsonVoicebotSessionStore)
        self.assertEqual([session.session_id for session in reloaded.list()], ["session-1"])

    def test_runtime_builder_can_select_memory_voicebot_session_store(self) -> None:
        settings = Settings(voicebot_session_store_provider="memory")

        store = build_voicebot_session_store(settings)

        self.assertIsInstance(store, VoicebotSessionStore)
        self.assertNotIsInstance(store, JsonVoicebotSessionStore)

    def test_runtime_builder_selects_json_agent_task_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                agent_task_store_provider="json",
                agent_task_store_path=f"{directory}/agent_tasks.json",
                agent_task_responded_event_retention=5,
            )

            tracker = build_agent_task_tracker(settings)
            tracker.mark_responded(7)
            reloaded = build_agent_task_tracker(settings)

        self.assertIsInstance(tracker, JsonAgentTaskTracker)
        self.assertEqual(reloaded.snapshot()["responded_event_ids"], [7])
        self.assertEqual(reloaded.snapshot()["responded_event_id_retention"], 5)

    def test_runtime_builder_can_select_memory_agent_task_tracker(self) -> None:
        settings = Settings(agent_task_store_provider="memory")

        tracker = build_agent_task_tracker(settings)

        self.assertIsInstance(tracker, AgentTaskTracker)
        self.assertNotIsInstance(tracker, JsonAgentTaskTracker)


if __name__ == "__main__":
    unittest.main()
