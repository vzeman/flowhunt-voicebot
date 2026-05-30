from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from voicebot.agent_tasks import AgentTaskTracker, JsonAgentTaskTracker
from voicebot.call_state import CallStateStore, JsonCallStateStore
from voicebot.events import EventStore, JsonEventStore
from voicebot.storage import (
    FilesystemArtifactStore,
    StorageConflict,
    StorageError,
    StorageNotFound,
    StorageUnavailable,
    storage_component_health,
)

from storage_contract_cases import (
    assert_agent_task_store_contract,
    assert_artifact_store_contract,
    assert_call_state_store_contract,
    assert_event_store_contract,
)


class StorageContractTests(unittest.TestCase):
    def test_memory_event_store_contract(self) -> None:
        assert_event_store_contract(self, lambda: EventStore(max_context_events=20))

    def test_json_event_store_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            assert_event_store_contract(self, lambda: JsonEventStore(path, max_context_events=20))

    def test_memory_call_state_store_contract(self) -> None:
        assert_call_state_store_contract(self, CallStateStore)

    def test_json_call_state_store_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.json"
            assert_call_state_store_contract(self, lambda: JsonCallStateStore(path))

    def test_memory_agent_task_store_contract(self) -> None:
        assert_agent_task_store_contract(self, AgentTaskTracker)

    def test_json_agent_task_store_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_tasks.json"
            assert_agent_task_store_contract(self, lambda: JsonAgentTaskTracker(path))

    def test_filesystem_artifact_store_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            assert_artifact_store_contract(self, lambda: FilesystemArtifactStore(directory))

    def test_storage_health_reports_recovery_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calls.json"
            path.write_text('{"calls":[{"state":"active"}]}', encoding="utf-8")
            store = JsonCallStateStore(path)

            health = storage_component_health(store)

        self.assertTrue(health.ok)
        self.assertEqual(health.message, "storage is reachable with recovery warnings")
        self.assertEqual(health.details["warning_count"], 1)

    def test_storage_error_payloads_are_structured(self) -> None:
        error = StorageConflict(
            "lease already owned",
            family="session_leases",
            driver="redis",
            details={"owner": "worker-1"},
        )

        self.assertIsInstance(error, StorageError)
        self.assertEqual(error.to_dict()["code"], "conflict")
        self.assertEqual(error.to_dict()["family"], "session_leases")
        self.assertEqual(error.to_dict()["details"], {"owner": "worker-1"})
        self.assertEqual(StorageUnavailable("down").code, "unavailable")
        self.assertEqual(StorageNotFound("missing").code, "not_found")


if __name__ == "__main__":
    unittest.main()
