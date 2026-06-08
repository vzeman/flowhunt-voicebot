from __future__ import annotations

import json
import tempfile
import unittest

from voicebot.workspace_model import (
    ChannelResolver,
    JsonVoicebotSessionStore,
    PublicVoicebotRoute,
    PublicVoicebotRouteStore,
    VoicebotChannelBinding,
    VoicebotSessionRecord,
    VoicebotSessionStore,
    WorkspaceScope,
    require_same_workspace,
)
from voicebot.storage import SQLiteVoicebotSessionStore


class WorkspaceModelTests(unittest.TestCase):
    def test_scope_requires_workspace_and_voicebot(self) -> None:
        with self.assertRaisesRegex(ValueError, "workspace_id"):
            WorkspaceScope("", "voicebot-1")
        with self.assertRaisesRegex(ValueError, "voicebot_id"):
            WorkspaceScope("workspace-1", "")

    def test_scope_adds_workspace_voicebot_and_session_to_event_data(self) -> None:
        scope = WorkspaceScope("workspace-1", "voicebot-1", session_id="session-1")

        self.assertEqual(
            scope.event_data(),
            {"workspace_id": "workspace-1", "voicebot_id": "voicebot-1", "session_id": "session-1"},
        )

    def test_task_dedupe_key_is_session_scoped(self) -> None:
        scope = WorkspaceScope("workspace-1", "voicebot-1", session_id="session-1")

        self.assertEqual(scope.task_dedupe_key(42), "session-1:42")

    def test_channel_resolver_maps_inbound_channel_to_workspace_and_voicebot(self) -> None:
        resolver = ChannelResolver(
            [
                VoicebotChannelBinding(
                    channel_id="channel-1",
                    kind="sip_trunk",
                    workspace_id="workspace-1",
                    voicebot_id="voicebot-1",
                    external_id="trunk-1",
                )
            ]
        )

        scope = resolver.resolve("sip_trunk", "trunk-1")

        self.assertEqual(scope, WorkspaceScope("workspace-1", "voicebot-1"))

    def test_channel_resolver_ignores_disabled_bindings(self) -> None:
        resolver = ChannelResolver(
            [
                VoicebotChannelBinding(
                    channel_id="channel-1",
                    kind="webrtc_widget",
                    workspace_id="workspace-1",
                    voicebot_id="voicebot-1",
                    external_id="widget-1",
                    enabled=False,
                )
            ]
        )

        self.assertIsNone(resolver.resolve("webrtc_widget", "widget-1"))

    def test_channel_binding_rejects_invalid_identity_fields(self) -> None:
        valid = {
            "channel_id": "channel-1",
            "kind": "sip_trunk",
            "workspace_id": "workspace-1",
            "voicebot_id": "voicebot-1",
            "external_id": "trunk-1",
        }

        for field, message in (
            ("channel_id", "channel_id"),
            ("workspace_id", "workspace_id"),
            ("voicebot_id", "voicebot_id"),
            ("external_id", "external_id"),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, message):
                    VoicebotChannelBinding(**{**valid, field: " "})

        with self.assertRaisesRegex(ValueError, "unsupported channel kind"):
            VoicebotChannelBinding(**{**valid, "kind": "fax"})

    def test_channel_resolver_unregisters_route_binding(self) -> None:
        resolver = ChannelResolver()
        binding = VoicebotChannelBinding(
            channel_id="channel-1",
            kind="sip_trunk",
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            external_id="trunk-1",
        )
        resolver.register(binding)

        removed = resolver.unregister("sip_trunk", "trunk-1")

        self.assertEqual(removed, binding)
        self.assertIsNone(resolver.resolve("sip_trunk", "trunk-1"))

    def test_channel_resolver_unregisters_by_channel_id(self) -> None:
        resolver = ChannelResolver(
            [
                VoicebotChannelBinding(
                    channel_id="channel-1",
                    kind="webrtc_widget",
                    workspace_id="workspace-1",
                    voicebot_id="voicebot-1",
                    external_id="widget-1",
                )
            ]
        )

        removed = resolver.unregister_channel("channel-1")

        self.assertEqual(removed.external_id if removed else None, "widget-1")
        self.assertIsNone(resolver.resolve("webrtc_widget", "widget-1"))

    def test_channel_resolver_rejects_route_reassignment(self) -> None:
        resolver = ChannelResolver()
        resolver.register(
            VoicebotChannelBinding(
                channel_id="channel-1",
                kind="sip_trunk",
                workspace_id="workspace-1",
                voicebot_id="voicebot-1",
                external_id="trunk-1",
            )
        )

        with self.assertRaisesRegex(ValueError, "another channel"):
            resolver.register(
                VoicebotChannelBinding(
                    channel_id="channel-2",
                    kind="sip_trunk",
                    workspace_id="workspace-1",
                    voicebot_id="voicebot-1",
                    external_id="trunk-1",
                )
            )

    def test_channel_resolver_rejects_channel_moves(self) -> None:
        resolver = ChannelResolver()
        resolver.register(
            VoicebotChannelBinding(
                channel_id="channel-1",
                kind="sip_trunk",
                workspace_id="workspace-1",
                voicebot_id="voicebot-1",
                external_id="trunk-1",
            )
        )

        with self.assertRaisesRegex(ValueError, "across routes"):
            resolver.register(
                VoicebotChannelBinding(
                    channel_id="channel-1",
                    kind="sip_trunk",
                    workspace_id="workspace-1",
                    voicebot_id="voicebot-1",
                    external_id="trunk-2",
                )
            )
        with self.assertRaisesRegex(ValueError, "across workspaces"):
            resolver.register(
                VoicebotChannelBinding(
                    channel_id="channel-1",
                    kind="sip_trunk",
                    workspace_id="workspace-2",
                    voicebot_id="voicebot-1",
                    external_id="trunk-1",
                )
            )
        with self.assertRaisesRegex(ValueError, "across voicebots"):
            resolver.register(
                VoicebotChannelBinding(
                    channel_id="channel-1",
                    kind="sip_trunk",
                    workspace_id="workspace-1",
                    voicebot_id="voicebot-2",
                    external_id="trunk-1",
                )
            )

    def test_cross_workspace_operation_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cross-workspace"):
            require_same_workspace(WorkspaceScope("workspace-1", "voicebot-1"), "workspace-2")

    def test_public_voicebot_route_store_resolves_host_and_longest_path_prefix(self) -> None:
        routes = PublicVoicebotRouteStore(
            [
                PublicVoicebotRoute(
                    "route-root",
                    "workspace-1",
                    "voicebot-root",
                    "channel-root",
                    "Voice.Example.com:443",
                    "/",
                    status="active",
                ),
                PublicVoicebotRoute(
                    "route-sales",
                    "workspace-1",
                    "voicebot-sales",
                    "channel-sales",
                    "voice.example.com",
                    "/voicebot/sales/",
                    status="active",
                ),
            ]
        )

        resolved = routes.resolve("https://VOICE.example.com", "/voicebot/sales/webrtc/sessions")

        self.assertEqual(resolved.route_id if resolved else None, "route-sales")
        self.assertEqual(resolved.host if resolved else None, "voice.example.com")
        self.assertEqual(resolved.path_prefix if resolved else None, "/voicebot/sales")

    def test_public_voicebot_route_store_rejects_duplicate_active_host_path(self) -> None:
        routes = PublicVoicebotRouteStore()
        routes.save(
            PublicVoicebotRoute(
                "route-1",
                "workspace-1",
                "voicebot-1",
                "channel-1",
                "voice.example.com",
                "/support",
                status="active",
            )
        )

        with self.assertRaisesRegex(ValueError, "conflicts"):
            routes.save(
                PublicVoicebotRoute(
                    "route-2",
                    "workspace-2",
                    "voicebot-2",
                    "channel-2",
                    "voice.example.com",
                    "/support/",
                    status="active",
                )
            )

    def test_public_voicebot_route_store_allows_disabled_duplicate_host_path(self) -> None:
        routes = PublicVoicebotRouteStore()
        routes.save(
            PublicVoicebotRoute(
                "route-1",
                "workspace-1",
                "voicebot-1",
                "channel-1",
                "voice.example.com",
                "/support",
                status="active",
            )
        )

        saved = routes.save(
            PublicVoicebotRoute(
                "route-2",
                "workspace-2",
                "voicebot-2",
                "channel-2",
                "voice.example.com",
                "/support",
                status="disabled",
            )
        )

        self.assertEqual(saved.route_id, "route-2")

    def test_session_record_carries_workspace_voicebot_and_session_scope(self) -> None:
        session = VoicebotSessionRecord("session-1", "workspace-1", "voicebot-1", channel_id="channel-1")

        self.assertEqual(session.scope(), WorkspaceScope("workspace-1", "voicebot-1", "session-1"))
        self.assertEqual(session.as_dict()["channel_id"], "channel-1")

    def test_session_record_rejects_invalid_status_and_timestamps(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported voicebot session status"):
            VoicebotSessionRecord("session-1", "workspace-1", "voicebot-1", status="paused")
        with self.assertRaisesRegex(ValueError, "started_at"):
            VoicebotSessionRecord("session-1", "workspace-1", "voicebot-1", started_at="2026-05-28T12:00:00")
        with self.assertRaisesRegex(ValueError, "ended_at is required"):
            VoicebotSessionRecord("session-1", "workspace-1", "voicebot-1", status="ended")
        with self.assertRaisesRegex(ValueError, "ended_at"):
            VoicebotSessionRecord(
                "session-1",
                "workspace-1",
                "voicebot-1",
                status="ended",
                ended_at="2026-05-28T12:00:00",
            )

    def test_session_store_lists_active_sessions_by_workspace_and_voicebot(self) -> None:
        store = VoicebotSessionStore()
        first = store.save(VoicebotSessionRecord("session-1", "workspace-1", "voicebot-1"))
        store.save(VoicebotSessionRecord("session-2", "workspace-1", "voicebot-2"))
        store.save(VoicebotSessionRecord("session-3", "workspace-2", "voicebot-1"))
        ended = store.end("session-1", "workspace-1")

        self.assertEqual(ended.status, "ended")
        self.assertEqual(store.get("session-1", "workspace-2"), None)
        self.assertEqual([session.session_id for session in store.list("workspace-1")], ["session-1", "session-2"])
        self.assertEqual(store.list("workspace-1", "voicebot-1", active_only=True), ())
        self.assertEqual(first.scope().task_dedupe_key(7), "session-1:7")

    def test_session_store_rejects_cross_workspace_move(self) -> None:
        store = VoicebotSessionStore()
        store.save(VoicebotSessionRecord("session-1", "workspace-1", "voicebot-1"))

        with self.assertRaisesRegex(ValueError, "across workspaces"):
            store.save(VoicebotSessionRecord("session-1", "workspace-2", "voicebot-1"))

    def test_session_store_rejects_cross_voicebot_move(self) -> None:
        store = VoicebotSessionStore()
        store.save(VoicebotSessionRecord("session-1", "workspace-1", "voicebot-1"))

        with self.assertRaisesRegex(ValueError, "across voicebots"):
            store.save(VoicebotSessionRecord("session-1", "workspace-1", "voicebot-2"))

    def test_json_session_store_persists_sessions_and_reload_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/sessions.json"
            first = JsonVoicebotSessionStore(path)
            first.save(VoicebotSessionRecord("session-1", "workspace-1", "voicebot-1", channel_id="channel-1"))
            ended = first.end("session-1", "workspace-1")

            reloaded = JsonVoicebotSessionStore(path)

        self.assertEqual(ended.status, "ended")
        self.assertEqual(reloaded.load_diagnostics["loaded_sessions"], 1)
        self.assertEqual(reloaded.get("session-1", "workspace-1").status, "ended")
        self.assertEqual(reloaded.get("session-1").channel_id, "channel-1")

    def test_sqlite_session_store_persists_sessions_and_filters_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite:///{directory}/sessions.sqlite3"
            first = SQLiteVoicebotSessionStore(database_url)
            first.save(VoicebotSessionRecord("session-1", "workspace-1", "voicebot-1", channel_id="channel-1"))
            first.save(VoicebotSessionRecord("session-2", "workspace-1", "voicebot-2"))
            ended = first.end("session-1", "workspace-1")
            first.close()

            reloaded = SQLiteVoicebotSessionStore(database_url)
            try:
                self.assertEqual(ended.status, "ended")
                self.assertEqual(reloaded.get("session-1", "workspace-1").status, "ended")
                self.assertEqual(reloaded.get("session-1").channel_id, "channel-1")
                self.assertEqual([session.session_id for session in reloaded.list("workspace-1")], ["session-1", "session-2"])
                self.assertEqual(reloaded.list("workspace-1", "voicebot-1", active_only=True), ())
            finally:
                reloaded.close()

    def test_sqlite_session_store_rejects_scope_moves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteVoicebotSessionStore(f"sqlite:///{directory}/sessions.sqlite3")
            try:
                store.save(VoicebotSessionRecord("session-1", "workspace-1", "voicebot-1"))
                with self.assertRaisesRegex(ValueError, "across workspaces"):
                    store.save(VoicebotSessionRecord("session-1", "workspace-2", "voicebot-1"))
                with self.assertRaisesRegex(ValueError, "across voicebots"):
                    store.save(VoicebotSessionRecord("session-1", "workspace-1", "voicebot-2"))
            finally:
                store.close()

    def test_json_session_store_skips_invalid_and_duplicate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/sessions.json"
            session = VoicebotSessionRecord("session-1", "workspace-1", "voicebot-1")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 1,
                        "sessions": [
                            session.as_dict(),
                            {**session.as_dict(), "workspace_id": "workspace-2"},
                            {"session_id": "bad"},
                        ],
                    },
                    handle,
                )

            reloaded = JsonVoicebotSessionStore(path)

        self.assertEqual(reloaded.load_diagnostics["loaded_sessions"], 1)
        self.assertEqual(reloaded.load_diagnostics["skipped_duplicate_session_ids"], 1)
        self.assertEqual(reloaded.load_diagnostics["skipped_invalid_sessions"], 1)
        self.assertEqual([session.session_id for session in reloaded.list("workspace-1")], ["session-1"])

    def test_json_session_store_reports_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/sessions.json"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{bad json}")

            reloaded = JsonVoicebotSessionStore(path)

        self.assertEqual(reloaded.load_diagnostics["skipped_malformed_json"], 1)
        self.assertEqual(reloaded.list(), ())


if __name__ == "__main__":
    unittest.main()
