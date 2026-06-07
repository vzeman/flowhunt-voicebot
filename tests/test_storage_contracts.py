from __future__ import annotations

from pathlib import Path
import tempfile
import time
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
from voicebot.storage.redis_agent_tasks import RedisAgentTaskTracker
from voicebot.storage.redis_call_state import RedisCallStateStore

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

    def test_redis_call_state_store_contract(self) -> None:
        assert_call_state_store_contract(
            self,
            lambda: RedisCallStateStore("redis://test", client=FakeRedis()),
        )

    def test_memory_agent_task_store_contract(self) -> None:
        assert_agent_task_store_contract(self, AgentTaskTracker)

    def test_json_agent_task_store_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent_tasks.json"
            assert_agent_task_store_contract(self, lambda: JsonAgentTaskTracker(path))

    def test_redis_agent_task_store_contract(self) -> None:
        assert_agent_task_store_contract(
            self,
            lambda: RedisAgentTaskTracker("redis://test", client=FakeRedis()),
        )

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


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, float | None]] = {}

    def ping(self) -> bool:
        return True

    def get(self, key: str) -> str | None:
        self._expire()
        item = self.values.get(key)
        return item[0] if item is not None else None

    def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool:
        self._expire()
        if nx and key in self.values:
            return False
        if px is not None:
            expires_at = time.monotonic() + (px / 1000)
        elif ex is not None:
            expires_at = time.monotonic() + ex
        else:
            expires_at = None
        self.values[key] = (value, expires_at)
        return True

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.values:
                removed += 1
            self.values.pop(key, None)
        return removed

    def keys(self, pattern: str) -> list[str]:
        self._expire()
        prefix = pattern.rstrip("*")
        return [key for key in self.values if key.startswith(prefix)]

    def ttl(self, key: str) -> int:
        self._expire()
        item = self.values.get(key)
        if item is None:
            return -2
        expires_at = item[1]
        if expires_at is None:
            return -1
        return max(0, int(expires_at - time.monotonic()))

    def _expire(self) -> None:
        now = time.monotonic()
        expired = [key for key, (_value, expires_at) in self.values.items() if expires_at is not None and expires_at <= now]
        for key in expired:
            self.values.pop(key, None)


if __name__ == "__main__":
    unittest.main()
