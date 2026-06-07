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
from voicebot.session_ownership import audit_session_ownership
from voicebot.transcripts import TranscriptStore


class FakeSession:
    def __init__(
        self,
        call_id: str = "call-1",
        session_id: str = "session-1",
        workspace_id: str = "workspace-1",
        voicebot_id: str = "voicebot-1",
    ) -> None:
        self.call_id = call_id
        self.session_id = session_id
        self.workspace_id = workspace_id
        self.voicebot_id = voicebot_id
        self.stopped = False

    def snapshot(self) -> dict:
        return {
            "call_id": self.call_id,
            "session_id": self.session_id,
            "transport": "webrtc",
            "route": {"workspace_id": self.workspace_id, "voicebot_id": self.voicebot_id},
        }

    def stop(self) -> None:
        self.stopped = True


class SessionLeaseStoreTests(unittest.TestCase):
    def test_session_lease_store_acquires_renews_releases_and_expires(self) -> None:
        store = SessionLeaseStore()
        now = datetime(2026, 5, 29, tzinfo=UTC)

        lease = store.acquire(
            "workspace-1",
            "voicebot-1",
            "session-1",
            "worker-1",
            10,
            call_id="call-1",
            transport="webrtc",
            metadata={"pod": "voicebot-1"},
            now=now,
        )
        blocked = store.acquire("workspace-1", "voicebot-1", "session-1", "worker-2", 10, now=now)
        renewed = store.renew("workspace-1", "voicebot-1", "session-1", "worker-1", 20, now=now)
        wrong_release = store.release("workspace-1", "voicebot-1", "session-1", owner="worker-2")
        released = store.release("workspace-1", "voicebot-1", "session-1", owner="worker-1")
        reacquired = store.acquire("workspace-1", "voicebot-1", "session-1", "worker-2", 1, now=now)
        expired = store.expire(now + timedelta(seconds=2))

        self.assertEqual(lease.owner if lease else None, "worker-1")
        self.assertEqual(lease.call_id if lease else None, "call-1")
        self.assertEqual(lease.transport if lease else None, "webrtc")
        self.assertEqual(lease.metadata if lease else None, {"pod": "voicebot-1"})
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
                    {{"workspace_id": "workspace-1", "voicebot_id": "voicebot-1", "session_id": "session-1", "owner": "worker-1", "expires_at": "{current}", "call_id": "call-1", "transport": "webrtc", "metadata": {{"pod": "voicebot-1"}}}},
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


class SessionOwnershipAuditTests(unittest.TestCase):
    def test_audit_session_ownership_classifies_owned_missing_and_mismatched_sessions(self) -> None:
        leases = SessionLeaseStore()
        leases.acquire("workspace-1", "voicebot-1", "session-1", "worker-1", 30, call_id="call-1")
        leases.acquire("workspace-1", "voicebot-1", "session-2", "worker-2", 30, call_id="call-2")
        snapshots = [
            FakeSession("call-1", "session-1").snapshot(),
            FakeSession("call-2", "session-2").snapshot(),
            FakeSession("call-3", "session-3").snapshot(),
            {"call_id": "call-4"},
        ]

        rows = audit_session_ownership(snapshots, leases, expected_owner="worker-1")

        self.assertEqual([row["status"] for row in rows], ["owned", "owner_mismatch", "missing", "unscoped"])
        self.assertEqual(rows[1]["current_owner"], "worker-2")
        self.assertEqual(rows[2]["reason"], "lease_missing")


class SessionLeaseApiTests(unittest.TestCase):
    def test_session_ownership_api_reports_owner_drift_without_stopping_sessions(self) -> None:
        registry = CallRegistry()
        owned = FakeSession("call-1", "session-1")
        mismatched = FakeSession("call-2", "session-2")
        missing = FakeSession("call-3", "session-3")
        registry.add(owned)
        registry.add(mismatched)
        registry.add(missing)
        leases = SessionLeaseStore()
        leases.acquire("workspace-1", "voicebot-1", "session-1", "worker-1", 30, call_id="call-1")
        leases.acquire("workspace-1", "voicebot-1", "session-2", "worker-2", 30, call_id="call-2")
        app = create_app(
            EventStore(max_context_events=20),
            registry,
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
            session_leases=leases,
        )
        client = TestClient(app)

        response = client.get("/scaling/session-ownership?expected_owner=worker-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"], {"total": 3, "owned": 1, "missing": 1, "owner_mismatch": 1, "unscoped": 0})
        self.assertEqual([session["status"] for session in response.json()["sessions"]], ["owned", "owner_mismatch", "missing"])
        self.assertFalse(owned.stopped)
        self.assertFalse(mismatched.stopped)
        self.assertFalse(missing.stopped)

    def test_session_lease_api_acquires_renews_releases_and_lists(self) -> None:
        events = EventStore(max_context_events=20)
        app = create_app(
            events,
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
            "call_id": "call-1",
            "transport": "webrtc",
            "metadata": {"pod": "voicebot-1"},
        }

        acquired = client.post("/scaling/session-leases/acquire", json=request)
        blocked = client.post("/scaling/session-leases/acquire", json={**request, "owner": "worker-2"})
        listed = client.get("/scaling/session-leases?workspace_id=workspace-1&voicebot_id=voicebot-1")
        renewed = client.post("/scaling/session-leases/renew", json=request)
        released = client.post("/scaling/session-leases/release", json={**request, "owner": "worker-1"})

        self.assertTrue(acquired.json()["acquired"])
        self.assertEqual(acquired.json()["lease"]["call_id"], "call-1")
        self.assertEqual(acquired.json()["lease"]["transport"], "webrtc")
        self.assertFalse(blocked.json()["acquired"])
        self.assertEqual([lease["session_id"] for lease in listed.json()["leases"]], ["session-1"])
        self.assertTrue(renewed.json()["renewed"])
        self.assertTrue(released.json()["released"])
        self.assertEqual(
            [event.type for event in events.list_events(call_id="call-1")],
            ["session_lease_acquired", "session_lease_renewed", "session_lease_released"],
        )

    def test_session_lease_api_expires_and_enforces_active_session_ownership(self) -> None:
        events = EventStore(max_context_events=50)
        registry = CallRegistry()
        session = FakeSession("call-1")
        registry.add(session)
        app = create_app(
            events,
            registry,
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        client = TestClient(app)

        expired = client.post("/scaling/session-leases/expire")
        enforced = client.post(
            "/scaling/session-leases/enforce",
            json={"owner": "worker-1", "stop_unleased_sessions": True, "recover_non_media_work": True},
        )

        self.assertEqual(expired.status_code, 200)
        self.assertEqual(expired.json()["expired"], [])
        self.assertEqual(enforced.status_code, 200)
        self.assertTrue(session.stopped)
        self.assertEqual([event["type"] for event in enforced.json()["recovered"]], ["session_recovered"])
        self.assertEqual([event["type"] for event in enforced.json()["interrupted"]], ["session_interrupted"])
        event_types = [event.type for event in events.list_events(call_id="call-1")]
        self.assertIn("session_lease_lost", event_types)
        self.assertIn("session_interrupted", event_types)


if __name__ == "__main__":
    unittest.main()
