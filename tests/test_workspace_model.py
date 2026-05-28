from __future__ import annotations

import unittest

from voicebot.workspace_model import (
    ChannelResolver,
    VoicebotChannelBinding,
    VoicebotSessionRecord,
    VoicebotSessionStore,
    WorkspaceScope,
    require_same_workspace,
)


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

    def test_session_record_carries_workspace_voicebot_and_session_scope(self) -> None:
        session = VoicebotSessionRecord("session-1", "workspace-1", "voicebot-1", channel_id="channel-1")

        self.assertEqual(session.scope(), WorkspaceScope("workspace-1", "voicebot-1", "session-1"))
        self.assertEqual(session.as_dict()["channel_id"], "channel-1")

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


if __name__ == "__main__":
    unittest.main()
