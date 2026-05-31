from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.security_contract import redact_sensitive_data, security_contract_issues, security_contract_payload
from voicebot.transcripts import TranscriptStore
from voicebot.workspace_access import WorkspaceAccessPolicy


class SecurityContractTests(unittest.TestCase):
    def test_redaction_recurses_through_nested_sensitive_keys(self) -> None:
        payload = {
            "token": "abc",
            "nested": {"api_key": "secret", "safe": "value"},
            "items": [{"password": "pw"}],
        }

        redacted = redact_sensitive_data(payload)

        self.assertEqual(redacted["token"], {"configured": True, "redacted": True})
        self.assertEqual(redacted["nested"]["api_key"], {"configured": True, "redacted": True})
        self.assertEqual(redacted["nested"]["safe"], "value")
        self.assertEqual(redacted["items"][0]["password"], {"configured": True, "redacted": True})

    def test_production_mode_requires_workspace_access_control(self) -> None:
        settings = Settings(deployment_mode="production", workspace_access_control_enabled=False)
        policy = WorkspaceAccessPolicy(enabled=False)

        issues = security_contract_issues(settings, policy)

        self.assertEqual(issues[0]["component"], "workspace_access")
        self.assertIn("internal_api_auth", {issue["component"] for issue in issues})

    def test_internal_auth_status_is_exposed_without_secret_values(self) -> None:
        settings = Settings(internal_auth_enabled=True, internal_api_keys=("admin:svc:secret:internal:*",))
        payload = security_contract_payload(settings, WorkspaceAccessPolicy(enabled=False))

        self.assertTrue(payload["internal_api_auth"]["enabled"])
        self.assertEqual(payload["internal_api_auth"]["configured_key_count"], 1)
        self.assertNotIn("secret", str(payload["internal_api_auth"]))

    def test_security_contract_exposes_retention_classes(self) -> None:
        settings = Settings()
        payload = security_contract_payload(settings, WorkspaceAccessPolicy(enabled=False))

        self.assertEqual(payload["mode"], "local_permissive")
        self.assertFalse(payload["secret_handling"]["raw_secret_api_responses"])
        self.assertIn("cached_tts_audio", {item["name"] for item in payload["retention"]["classes"]})


class SecurityApiTests(unittest.TestCase):
    def build_client(self, policy: WorkspaceAccessPolicy | None = None) -> TestClient:
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

    def test_security_contract_endpoint_exposes_issues_and_contract(self) -> None:
        response = self.build_client().get("/security/contract")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["issues"], [])
        self.assertEqual(response.json()["contract"]["mode"], "local_permissive")

    def test_workspace_retention_endpoint_enforces_workspace_access(self) -> None:
        client = self.build_client(WorkspaceAccessPolicy(enabled=True, allowed_workspace_ids=("workspace-1",)))

        allowed = client.get("/workspaces/workspace-1/security/retention")
        denied = client.get("/workspaces/workspace-2/security/retention")

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 403)

    def test_workspace_audit_endpoint_redacts_sensitive_payload(self) -> None:
        client = self.build_client(WorkspaceAccessPolicy(enabled=True, allowed_workspace_ids=("workspace-1",)))

        response = client.post(
            "/workspaces/workspace-1/security/audit",
            json={
                "action": "provider_config_change",
                "actor": "test",
                "voicebot_id": "voicebot-1",
                "metadata": {"api_key": "secret", "safe": "value"},
            },
        )

        self.assertEqual(response.status_code, 200)
        event = response.json()["event"]
        self.assertEqual(event["type"], "security_audit")
        self.assertEqual(event["data"]["workspace_id"], "workspace-1")
        self.assertEqual(event["data"]["metadata"]["api_key"], {"configured": True, "redacted": True})
        self.assertEqual(event["data"]["metadata"]["safe"], "value")


if __name__ == "__main__":
    unittest.main()
