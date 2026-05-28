from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.sip_trunks import SipTrunkStore
from voicebot.transcripts import TranscriptStore


class FakeAsterisk:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def reload_pjsip(self):
        self.commands.append("pjsip reload")
        return FakeControlResult(True, "reloaded")

    def send_register(self, registration: str):
        self.commands.append(f"pjsip send register {registration}")
        return FakeControlResult(True, f"registered {registration}")

    def send_unregister(self, registration: str):
        self.commands.append(f"pjsip send unregister {registration}")
        return FakeControlResult(True, f"unregistered {registration}")

    def show_registrations(self):
        self.commands.append("pjsip show registrations")
        return FakeControlResult(True, "registration table")


class FakeControlResult:
    def __init__(self, ok: bool, message: str) -> None:
        self.ok = ok
        self.message = message


class ApiSipTrunkTests(unittest.TestCase):
    def build_client(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        asterisk = FakeAsterisk()
        store = SipTrunkStore(str(root / "sip_trunks.json"), str(root / "asterisk" / "pjsip-trunks.conf"))
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore(str(root / "transcripts")),
            asterisk,
            sip_trunks=store,
        )
        return directory, TestClient(app), asterisk, store

    def test_create_trunk_redacts_password_and_registers(self) -> None:
        directory, client, asterisk, _store = self.build_client()
        with directory:
            response = client.post(
                "/sip-trunks",
                json={
                    "trunk_id": "customer-1",
                    "host": "sip.example.com",
                    "user": "user-1",
                    "password": "secret",
                    "display_name": "Customer 1",
                },
            )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["trunk"]["password"], {"configured": True, "redacted": True})
            self.assertEqual(
                asterisk.commands,
                ["pjsip reload", "pjsip send register trunk-customer-1-reg"],
            )

    def test_disconnect_disables_trunk_and_unregisters_before_reload(self) -> None:
        directory, client, asterisk, store = self.build_client()
        with directory:
            client.post(
                "/sip-trunks",
                json={
                    "trunk_id": "customer-1",
                    "host": "sip.example.com",
                    "user": "user-1",
                    "password": "secret",
                },
            )
            asterisk.commands.clear()

            response = client.post("/sip-trunks/customer-1/disconnect")

            self.assertEqual(response.status_code, 200)
            self.assertFalse(store.get("customer-1").enabled)
            self.assertEqual(
                asterisk.commands,
                ["pjsip send unregister trunk-customer-1-reg", "pjsip reload"],
            )

    def test_delete_trunk_unregisters_and_removes_registry_entry(self) -> None:
        directory, client, asterisk, store = self.build_client()
        with directory:
            client.post(
                "/sip-trunks",
                json={
                    "trunk_id": "customer-1",
                    "host": "sip.example.com",
                    "user": "user-1",
                    "password": "secret",
                },
            )
            asterisk.commands.clear()

            response = client.delete("/sip-trunks/customer-1")

            self.assertEqual(response.status_code, 200)
            self.assertIsNone(store.get("customer-1"))
            self.assertEqual(
                asterisk.commands,
                ["pjsip send unregister trunk-customer-1-reg", "pjsip reload"],
            )


if __name__ == "__main__":
    unittest.main()
