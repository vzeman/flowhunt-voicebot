from __future__ import annotations

import tempfile
import unittest

from voicebot.events import EventStore, JsonEventStore, event_from_dict


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


if __name__ == "__main__":
    unittest.main()
