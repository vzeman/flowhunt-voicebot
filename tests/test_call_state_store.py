from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.call_state import CallStateStore, JsonCallStateStore
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.runtime_storage import build_call_state_store
from voicebot.config import Settings
from voicebot.transcripts import TranscriptStore


class CallStateStoreTests(unittest.TestCase):
    def test_memory_store_tracks_active_and_ended_call_snapshots(self) -> None:
        store = CallStateStore()

        active = store.upsert({"call_id": "call-1", "playback_active": True})
        ended = store.end("call-1")

        self.assertEqual(active["state"], "active")
        self.assertEqual(ended["state"], "ended")
        self.assertEqual(store.list(active_only=True), ())
        self.assertEqual(store.get("call-1")["state"], "ended")

    def test_json_store_reload_preserves_call_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calls.json"
            store = JsonCallStateStore(path)
            store.upsert({"call_id": "call-1", "recording": False, "playback_active": True})

            reloaded = JsonCallStateStore(path)

            self.assertEqual(reloaded.load_diagnostics["loaded_states"], 1)
            self.assertEqual(reloaded.get("call-1")["playback_active"], True)

    def test_json_store_skips_invalid_and_duplicate_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calls.json"
            path.write_text(
                """
                {
                  "calls": [
                    {"call_id": "call-1", "state": "active"},
                    {"call_id": "call-1", "state": "ended"},
                    {"state": "active"}
                  ]
                }
                """,
                encoding="utf-8",
            )

            store = JsonCallStateStore(path)

            self.assertEqual(store.load_diagnostics["loaded_states"], 1)
            self.assertEqual(store.load_diagnostics["skipped_duplicate_call_ids"], 1)
            self.assertEqual(store.load_diagnostics["skipped_invalid_states"], 1)

    def test_call_registry_writes_state_store(self) -> None:
        registry = CallRegistry()
        session = _FakeSession("call-1")

        registry.add(session)
        registry.snapshot("call-1")
        registry.remove("call-1")

        self.assertEqual(registry.stored_snapshots()[0]["state"], "ended")
        self.assertEqual(registry.stored_snapshots()[0]["snapshot_count"], 2)

    def test_build_call_state_store_supports_configured_providers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            json_store = build_call_state_store(Settings(call_state_store_provider="json", call_state_store_path=f"{tmp}/calls.json"))
            memory_store = build_call_state_store(Settings(call_state_store_provider="memory"))

        self.assertIsInstance(json_store, JsonCallStateStore)
        self.assertIsInstance(memory_store, CallStateStore)


class CallStateApiTests(unittest.TestCase):
    def test_call_state_store_endpoint_returns_persisted_states(self) -> None:
        registry = CallRegistry()
        registry.state_store.upsert({"call_id": "call-1", "playback_active": False})
        registry.state_store.end("call-1")
        registry.state_store.upsert({"call_id": "call-2", "playback_active": True})
        app = create_app(
            EventStore(max_context_events=20),
            registry,
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        client = TestClient(app)

        all_states = client.get("/calls/state-store")
        active_only = client.get("/calls/state-store?active_only=true")

        self.assertEqual(all_states.status_code, 200)
        self.assertEqual([item["call_id"] for item in all_states.json()["calls"]], ["call-1", "call-2"])
        self.assertEqual([item["call_id"] for item in active_only.json()["calls"]], ["call-2"])


class _FakeSession:
    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.snapshot_count = 0

    def snapshot(self) -> dict:
        self.snapshot_count += 1
        return {
            "call_id": self.call_id,
            "recording": False,
            "playback_active": False,
            "stopped": False,
            "snapshot_count": self.snapshot_count,
        }


if __name__ == "__main__":
    unittest.main()
