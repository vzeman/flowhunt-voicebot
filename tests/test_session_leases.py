from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.runtime_storage import build_session_lease_store
from voicebot.session_leases import JsonSessionLeaseStore, SessionLeaseStore
from voicebot.transcripts import TranscriptStore


class SessionLeaseStoreTests(unittest.TestCase):
    def test_session_lease_store_acquires_renews_releases_and_expires(self) -> None:
        store = SessionLeaseStore()
        now = datetime(2026, 5, 29, tzinfo=UTC)

        lease = store.acquire("workspace-1", "voicebot-1", "session-1", "worker-1", 10, now=now)
        blocked = store.acquire("workspace-1", "voicebot-1", "session-1", "worker-2", 10, now=now)
        renewed = store.renew("workspace-1", "voicebot-1", "session-1", "worker-1", 20, now=now)
        wrong_release = store.release("workspace-1", "voicebot-1", "session-1", owner="worker-2")
        released = store.release("workspace-1", "voicebot-1", "session-1", owner="worker-1")
        reacquired = store.acquire("workspace-1", "voicebot-1", "session-1", "worker-2", 1, now=now)
        expired = store.expire(now + timedelta(seconds=2))

        self.assertEqual(lease.owner if lease else None, "worker-1")
        self.assertIsNone(blocked)
        self.assertEqual(renewed.owner if renewed else None, "worker-1")
        self.assertIsNone(wrong_release)
        self.assertEqual(released.owner if released else None, "worker-1")
        self.assertEqual(reacquired.owner if reacquired else None, "worker-2")
        self.assertEqual([lease.owner for lease in expired], ["worker-2"])

    def test_json_session_lease_store_persists_unexpired_leases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leases.json"
            now = datetime.now(UTC)
            first = JsonSessionLeaseStore(path)
            first.acquire("workspace-1", "voicebot-1", "session-1", "worker-1", 60, now=now)

            reloaded = JsonSessionLeaseStore(path)

            self.assertEqual(reloaded.load_diagnostics["loaded_leases"], 1)
            self.assertEqual(reloaded.list()[0].owner, "worker-1")

    def test_json_session_lease_store_skips_invalid_duplicate_and_expired_leases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leases.json"
            now = datetime.now(UTC)
            expired = (now - timedelta(seconds=10)).isoformat()
            current = (now + timedelta(seconds=60)).isoformat()
            path.write_text(
                f"""
                {{
                  "leases": [
                    {{"workspace_id": "workspace-1", "voicebot_id": "voicebot-1", "session_id": "session-1", "owner": "worker-1", "expires_at": "{current}"}},
                    {{"workspace_id": "workspace-1", "voicebot_id": "voicebot-1", "session_id": "session-1", "owner": "worker-2", "expires_at": "{current}"}},
                    {{"workspace_id": "workspace-1", "voicebot_id": "voicebot-1", "session_id": "session-2", "owner": "worker-1", "expires_at": "{expired}"}},
                    {{"workspace_id": "", "voicebot_id": "voicebot-1", "session_id": "session-3", "owner": "worker-1", "expires_at": "{current}"}}
                  ]
                }}
                """,
                encoding="utf-8",
            )

            store = JsonSessionLeaseStore(path)

            self.assertEqual(store.load_diagnostics["loaded_leases"], 1)
            self.assertEqual(store.load_diagnostics["skipped_duplicate_lease_keys"], 1)
            self.assertEqual(store.load_diagnostics["skipped_expired_leases"], 1)
            self.assertEqual(store.load_diagnostics["skipped_invalid_leases"], 1)

    def test_session_lease_builder_supports_configured_providers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            json_store = build_session_lease_store(
                Settings(session_lease_store_provider="json", session_lease_store_path=f"{tmp}/leases.json")
            )
            memory_store = build_session_lease_store(Settings(session_lease_store_provider="memory"))

        self.assertIsInstance(json_store, JsonSessionLeaseStore)
        self.assertIsInstance(memory_store, SessionLeaseStore)


class SessionLeaseApiTests(unittest.TestCase):
    def test_session_lease_api_acquires_renews_releases_and_lists(self) -> None:
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        client = TestClient(app)
        request = {
            "workspace_id": "workspace-1",
            "voicebot_id": "voicebot-1",
            "session_id": "session-1",
            "owner": "worker-1",
            "ttl_seconds": 30,
        }

        acquired = client.post("/scaling/session-leases/acquire", json=request)
        blocked = client.post("/scaling/session-leases/acquire", json={**request, "owner": "worker-2"})
        listed = client.get("/scaling/session-leases?workspace_id=workspace-1&voicebot_id=voicebot-1")
        renewed = client.post("/scaling/session-leases/renew", json=request)
        released = client.post("/scaling/session-leases/release", json={**request, "owner": "worker-1"})

        self.assertTrue(acquired.json()["acquired"])
        self.assertFalse(blocked.json()["acquired"])
        self.assertEqual([lease["session_id"] for lease in listed.json()["leases"]], ["session-1"])
        self.assertTrue(renewed.json()["renewed"])
        self.assertTrue(released.json()["released"])


if __name__ == "__main__":
    unittest.main()
