from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.transcripts import TranscriptStore
from voicebot.workspace_access import WorkspaceAccessPolicy, workspace_access_policy_from_settings


class WorkspaceAccessPolicyTests(unittest.TestCase):
    def test_disabled_policy_allows_any_nonblank_workspace(self) -> None:
        WorkspaceAccessPolicy(enabled=False).require_workspace("workspace-1")

    def test_enabled_policy_allows_configured_workspace(self) -> None:
        WorkspaceAccessPolicy(enabled=True, allowed_workspace_ids=("workspace-1",)).require_workspace("workspace-1")

    def test_enabled_policy_rejects_unconfigured_workspace(self) -> None:
        with self.assertRaisesRegex(PermissionError, "workspace access denied"):
            WorkspaceAccessPolicy(enabled=True, allowed_workspace_ids=("workspace-1",)).require_workspace("workspace-2")

    def test_policy_rejects_blank_workspace(self) -> None:
        with self.assertRaisesRegex(ValueError, "workspace_id is required"):
            WorkspaceAccessPolicy(enabled=False).require_workspace(" ")

    def test_policy_can_be_loaded_from_settings(self) -> None:
        settings = Settings(workspace_access_control_enabled=True, allowed_workspace_ids=("workspace-1",))

        policy = workspace_access_policy_from_settings(settings)

        self.assertTrue(policy.enabled)
        self.assertEqual(policy.allowed_workspace_ids, ("workspace-1",))

class WorkspaceAccessApiTests(unittest.TestCase):
    def build_client(self, policy: WorkspaceAccessPolicy) -> TestClient:
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
            workspace_policy=policy,
        )
        return TestClient(app)

    def test_workspace_api_allows_configured_workspace(self) -> None:
        client = self.build_client(WorkspaceAccessPolicy(enabled=True, allowed_workspace_ids=("workspace-1",)))

        response = client.get("/workspaces/workspace-1/voicebots")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["workspace_id"], "workspace-1")

    def test_workspace_api_rejects_unconfigured_workspace(self) -> None:
        client = self.build_client(WorkspaceAccessPolicy(enabled=True, allowed_workspace_ids=("workspace-1",)))

        response = client.get("/workspaces/workspace-2/voicebots")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "workspace access denied")

    def test_workspace_api_checks_provider_and_session_routes(self) -> None:
        client = self.build_client(WorkspaceAccessPolicy(enabled=True, allowed_workspace_ids=("workspace-1",)))

        provider = client.get("/workspaces/workspace-2/voicebots/voicebot-1/providers")
        sessions = client.get("/workspaces/workspace-2/voicebots/voicebot-1/sessions")

        self.assertEqual(provider.status_code, 403)
        self.assertEqual(sessions.status_code, 403)


if __name__ == "__main__":
    unittest.main()
