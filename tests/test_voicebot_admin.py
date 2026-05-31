from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore
from voicebot.workspace_model import ChannelResolver, VoicebotDefinition, VoicebotStore


class VoicebotAdminTests(unittest.TestCase):
    def build_client(self) -> tuple[TestClient, VoicebotStore, ChannelResolver]:
        store = VoicebotStore()
        channels = ChannelResolver()
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
            voicebots=store,
            channels=channels,
        )
        return TestClient(app), store, channels

    def test_voicebot_admin_crud_is_workspace_scoped(self) -> None:
        client, _store, _channels = self.build_client()

        created = client.post(
            "/workspaces/workspace-1/voicebots",
            json={
                "voicebot_id": "voicebot-1",
                "display_name": "Support bot",
                "metadata": {"language": "en"},
            },
        )
        listed = client.get("/workspaces/workspace-1/voicebots")
        hidden = client.get("/workspaces/workspace-2/voicebots")
        read = client.get("/workspaces/workspace-1/voicebots/voicebot-1")
        patched = client.patch(
            "/workspaces/workspace-1/voicebots/voicebot-1",
            json={"enabled": False, "display_name": "Support bot v2"},
        )
        deleted = client.delete("/workspaces/workspace-1/voicebots/voicebot-1")
        missing = client.get("/workspaces/workspace-1/voicebots/voicebot-1")

        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["voicebot"]["workspace_id"], "workspace-1")
        self.assertEqual([item["voicebot_id"] for item in listed.json()["voicebots"]], ["voicebot-1"])
        self.assertEqual(hidden.json()["voicebots"], [])
        self.assertEqual(read.json()["voicebot"]["metadata"], {"language": "en"})
        self.assertFalse(patched.json()["voicebot"]["enabled"])
        self.assertEqual(patched.json()["voicebot"]["display_name"], "Support bot v2")
        self.assertTrue(deleted.json()["deleted"])
        self.assertEqual(missing.status_code, 404)

    def test_voicebot_admin_rejects_duplicates_and_invalid_records(self) -> None:
        client, store, _channels = self.build_client()
        store.create(VoicebotDefinition("workspace-1", "voicebot-1"))

        duplicate = client.post("/workspaces/workspace-1/voicebots", json={"voicebot_id": "voicebot-1"})
        invalid = client.post(
            "/workspaces/workspace-1/voicebots",
            json={"voicebot_id": " ", "display_name": "Invalid"},
        )
        missing_patch = client.patch("/workspaces/workspace-1/voicebots/missing", json={"enabled": False})

        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("already exists", duplicate.json()["detail"])
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("voicebot_id", invalid.json()["detail"])
        self.assertEqual(missing_patch.status_code, 404)

    def test_voicebot_channel_crud_uses_workspace_voicebot_scope(self) -> None:
        client, _store, channels = self.build_client()

        created = client.post(
            "/workspaces/workspace-1/voicebots/voicebot-1/channels",
            json={
                "channel_id": "channel-1",
                "kind": "sip_trunk",
                "external_id": "trunk-1",
                "metadata": {"country": "sk"},
            },
        )
        listed = client.get("/workspaces/workspace-1/voicebots/voicebot-1/channels")
        hidden = client.get("/workspaces/workspace-1/voicebots/voicebot-2/channels")
        read = client.get("/workspaces/workspace-1/voicebots/voicebot-1/channels/channel-1")
        patched = client.patch(
            "/workspaces/workspace-1/voicebots/voicebot-1/channels/channel-1",
            json={"enabled": False, "metadata": {"country": "cz"}},
        )
        deleted = client.delete("/workspaces/workspace-1/voicebots/voicebot-1/channels/channel-1")
        missing = client.get("/workspaces/workspace-1/voicebots/voicebot-1/channels/channel-1")

        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["channel"]["workspace_id"], "workspace-1")
        self.assertEqual(created.json()["channel"]["voicebot_id"], "voicebot-1")
        self.assertEqual([item["channel_id"] for item in listed.json()["channels"]], ["channel-1"])
        self.assertEqual(hidden.json()["channels"], [])
        self.assertEqual(read.json()["channel"]["external_id"], "trunk-1")
        self.assertFalse(patched.json()["channel"]["enabled"])
        self.assertEqual(patched.json()["channel"]["metadata"], {"country": "cz"})
        self.assertIsNone(channels.resolve("sip_trunk", "trunk-1"))
        self.assertTrue(deleted.json()["deleted"])
        self.assertEqual(missing.status_code, 404)

    def test_voicebot_channel_admin_rejects_invalid_and_conflicting_routes(self) -> None:
        client, _store, _channels = self.build_client()

        first = client.post(
            "/workspaces/workspace-1/voicebots/voicebot-1/channels",
            json={"channel_id": "channel-1", "kind": "sip_trunk", "external_id": "trunk-1"},
        )
        duplicate_route = client.post(
            "/workspaces/workspace-1/voicebots/voicebot-1/channels",
            json={"channel_id": "channel-2", "kind": "sip_trunk", "external_id": "trunk-1"},
        )
        invalid_kind = client.post(
            "/workspaces/workspace-1/voicebots/voicebot-1/channels",
            json={"channel_id": "channel-3", "kind": "fax", "external_id": "fax-1"},
        )
        missing_patch = client.patch(
            "/workspaces/workspace-1/voicebots/voicebot-1/channels/missing",
            json={"enabled": False},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate_route.status_code, 400)
        self.assertIn("another channel", duplicate_route.json()["detail"])
        self.assertEqual(invalid_kind.status_code, 400)
        self.assertIn("unsupported channel kind", invalid_kind.json()["detail"])
        self.assertEqual(missing_patch.status_code, 404)

    def test_public_route_admin_crud_is_workspace_voicebot_scoped(self) -> None:
        client, store, _channels = self.build_client()
        store.create(VoicebotDefinition("workspace-1", "voicebot-1"))
        client.post(
            "/workspaces/workspace-1/voicebots/voicebot-1/channels",
            json={"channel_id": "channel-1", "kind": "webrtc_widget", "external_id": "widget-1"},
        )

        created = client.post(
            "/workspaces/workspace-1/voicebots/voicebot-1/public-routes",
            json={
                "route_id": "route-1",
                "channel_id": "channel-1",
                "host": "Voice.Example.com:443",
                "path_prefix": "/support/",
                "status": "active",
                "allowed_origins": ["https://www.example.com"],
            },
        )
        listed = client.get("/workspaces/workspace-1/voicebots/voicebot-1/public-routes")
        patched = client.patch(
            "/workspaces/workspace-1/voicebots/voicebot-1/public-routes/route-1",
            json={"status": "disabled", "path_prefix": "/sales"},
        )
        deleted = client.delete("/workspaces/workspace-1/voicebots/voicebot-1/public-routes/route-1")

        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["route"]["host"], "voice.example.com")
        self.assertEqual(created.json()["route"]["path_prefix"], "/support")
        self.assertEqual(created.json()["route"]["channel_id"], "channel-1")
        self.assertEqual([route["route_id"] for route in listed.json()["routes"]], ["route-1"])
        self.assertEqual(patched.json()["route"]["status"], "disabled")
        self.assertEqual(patched.json()["route"]["path_prefix"], "/sales")
        self.assertTrue(deleted.json()["deleted"])

    def test_public_route_admin_rejects_missing_channel_and_duplicate_active_route(self) -> None:
        client, store, _channels = self.build_client()
        store.create(VoicebotDefinition("workspace-1", "voicebot-1"))
        client.post(
            "/workspaces/workspace-1/voicebots/voicebot-1/channels",
            json={"channel_id": "channel-1", "kind": "webrtc_widget", "external_id": "widget-1"},
        )
        first = client.post(
            "/workspaces/workspace-1/voicebots/voicebot-1/public-routes",
            json={
                "route_id": "route-1",
                "channel_id": "channel-1",
                "host": "voice.example.com",
                "path_prefix": "/support",
                "status": "active",
            },
        )
        missing_channel = client.post(
            "/workspaces/workspace-1/voicebots/voicebot-1/public-routes",
            json={
                "route_id": "route-2",
                "channel_id": "missing",
                "host": "missing.example.com",
                "status": "active",
            },
        )
        duplicate = client.post(
            "/workspaces/workspace-1/voicebots/voicebot-1/public-routes",
            json={
                "route_id": "route-3",
                "channel_id": "channel-1",
                "host": "voice.example.com",
                "path_prefix": "/support/",
                "status": "active",
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(missing_channel.status_code, 404)
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("conflicts", duplicate.json()["detail"])

    def test_voicebot_validate_reports_missing_and_ready_runtime_dependencies(self) -> None:
        client, _store, _channels = self.build_client()

        missing = client.post("/workspaces/workspace-1/voicebots/voicebot-1/validate")
        client.post("/workspaces/workspace-1/voicebots", json={"voicebot_id": "voicebot-1"})
        no_channel_or_provider = client.post("/workspaces/workspace-1/voicebots/voicebot-1/validate")
        client.post(
            "/workspaces/workspace-1/voicebots/voicebot-1/channels",
            json={"channel_id": "channel-1", "kind": "webrtc_widget", "external_id": "widget-1"},
        )
        client.put(
            "/workspaces/workspace-1/voicebots/voicebot-1/providers",
            json={
                "stt": {"provider": "whisper", "model": "base"},
                "tts": {"provider": "supertonic", "model": "supertonic-3"},
                "agent": {
                    "provider": "openai-responses",
                    "secret_ref": {"name": "openai-main"},
                },
            },
        )
        ready = client.post("/workspaces/workspace-1/voicebots/voicebot-1/validate")

        self.assertFalse(missing.json()["ok"])
        self.assertEqual(
            [issue["area"] for issue in missing.json()["issues"]],
            ["voicebot", "channel", "provider"],
        )
        self.assertFalse(no_channel_or_provider.json()["ok"])
        self.assertEqual(
            [issue["area"] for issue in no_channel_or_provider.json()["issues"]],
            ["channel", "provider"],
        )
        self.assertTrue(ready.json()["ok"])
        self.assertEqual(ready.json()["channel_count"], 1)
        self.assertEqual(ready.json()["selection_plan"]["providers"]["stt"], "whisper")


if __name__ == "__main__":
    unittest.main()
