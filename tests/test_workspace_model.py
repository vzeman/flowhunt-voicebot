from __future__ import annotations

import unittest

from voicebot.workspace_model import (
    ChannelResolver,
    VoicebotChannelBinding,
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

    def test_cross_workspace_operation_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cross-workspace"):
            require_same_workspace(WorkspaceScope("workspace-1", "voicebot-1"), "workspace-2")


if __name__ == "__main__":
    unittest.main()
