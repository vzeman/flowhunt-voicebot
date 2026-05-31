from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.internal_auth import parse_internal_api_keys
from voicebot.transcripts import TranscriptStore


class InternalAuthTests(unittest.TestCase):
    def build_client(self, settings: Settings) -> tuple[TestClient, EventStore]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        events = EventStore(max_context_events=50)
        app = create_app(
            events,
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(directory.name),
            None,
            settings=settings,
        )
        return TestClient(app), events

    def test_internal_auth_rejects_missing_and_invalid_keys(self) -> None:
        client, events = self.build_client(
            Settings(internal_auth_enabled=True, internal_api_keys=("admin:control:secret:internal:*",))
        )

        missing = client.get("/config")
        invalid = client.get("/config", headers={"X-FlowHunt-Internal-Key": "wrong"})

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()["detail"], "missing_internal_api_key")
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(invalid.json()["detail"], "invalid_internal_api_key")
        self.assertEqual(
            [event.type for event in events.list_events(call_id="system") if event.type == "internal_api_auth_denied"],
            ["internal_api_auth_denied", "internal_api_auth_denied"],
        )

    def test_access_log_events_are_structured_and_secret_safe(self) -> None:
        client, events = self.build_client(
            Settings(internal_auth_enabled=True, internal_api_keys=("admin:control:secret:internal:*",))
        )

        response = client.get(
            "/config",
            headers={
                "X-FlowHunt-Internal-Key": "secret",
                "X-Request-Id": "request-1",
                "User-Agent": "test-agent",
            },
        )

        self.assertEqual(response.status_code, 200)
        access = [event for event in events.list_events(call_id="access") if event.type == "api_access_logged"][-1]
        self.assertEqual(access.data["request_id"], "request-1")
        self.assertEqual(access.data["audience"], "internal")
        self.assertEqual(access.data["status_code"], 200)
        self.assertFalse(access.data["source_ip_recorded"])
        self.assertNotIn("secret", str(access.data))

    def test_internal_auth_accepts_valid_key_and_redacts_configured_keys(self) -> None:
        client, events = self.build_client(
            Settings(internal_auth_enabled=True, internal_api_keys=("admin:control:secret:internal:*",))
        )

        response = client.get("/config", headers={"X-FlowHunt-Internal-Key": "secret"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["settings"]["internal_api_keys"], {"configured": True, "redacted": True})
        accepted = [event for event in events.list_events(call_id="system") if event.type == "internal_api_auth_accepted"]
        self.assertEqual(accepted[0].data["key_id"], "admin")
        self.assertNotIn("secret", str(accepted[0].data))

    def test_internal_auth_enforces_scopes(self) -> None:
        client, _events = self.build_client(
            Settings(internal_auth_enabled=True, internal_api_keys=("agent-worker:agent:secret:agent:work",))
        )

        response = client.get("/config", headers={"X-FlowHunt-Internal-Key": "secret"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "insufficient_internal_api_key_scope")
        self.assertEqual(response.json()["scope"], "internal:read")

    def test_public_routes_do_not_require_internal_key(self) -> None:
        client, _events = self.build_client(
            Settings(internal_auth_enabled=True, internal_api_keys=("admin:control:secret:internal:*",))
        )

        health = client.get("/health")
        webrtc = client.post("/webrtc/sessions", json={"sdp": "offer-sdp", "type": "offer"})

        self.assertEqual(health.status_code, 200)
        self.assertEqual(webrtc.status_code, 503)
        self.assertNotEqual(webrtc.json()["detail"], "missing_internal_api_key")

    def test_key_parser_supports_plain_and_scoped_keys(self) -> None:
        keys = parse_internal_api_keys(("plain-secret", "agent-key:agent:agent-secret:agent:work|call:read"))

        self.assertEqual(keys[0].key_id, "key-1")
        self.assertTrue(keys[0].can_access("internal:read"))
        self.assertEqual(keys[1].key_id, "agent-key")
        self.assertFalse(keys[1].can_access("internal:read"))
        self.assertTrue(keys[1].can_access("call:read"))


if __name__ == "__main__":
    unittest.main()
