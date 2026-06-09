from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.runtime_storage import (
    build_agent_task_tracker,
    build_audio_artifact_store,
    build_call_state_store,
    build_event_store,
    build_provider_config_store,
    build_sip_trunk_store,
    build_subagent_task_store,
    build_transcript_store,
    build_voicebot_session_store,
    build_worker_queue_store,
    build_worker_registry,
    default_storage_registry,
    selected_storage_drivers,
    storage_drivers_payload,
)
from voicebot.storage import S3ArtifactStore, attached_storage_driver, normalize_driver_name
from voicebot.storage.redis_leases import RedisSessionLeaseStore
from voicebot.storage.redis_subagent_tasks import RedisSubagentTaskStore
from voicebot.storage.redis_worker_queue import RedisWorkerQueueStore
from voicebot.storage.redis_worker_registry import RedisWorkerRegistry
from voicebot.storage.sqlite_events import SQLiteEventStore
from voicebot.storage.sqlite_provider_config import SQLiteProviderConfigStore
from voicebot.storage.sqlite_sessions import SQLiteVoicebotSessionStore
from voicebot.storage.sqlite_sip_trunks import SQLiteSipTrunkStore
from voicebot.storage.sqlite_transcripts import SQLiteTranscriptStore
from voicebot.transcripts import TranscriptStore
from storage_contract_cases import assert_event_store_contract


class StorageDriverTests(unittest.TestCase):
    def test_default_registry_lists_local_and_managed_drivers(self) -> None:
        registry = default_storage_registry()

        event_definitions = {definition.driver: definition for definition in registry.definitions_for_family("events")}
        agent_task_definitions = {definition.driver: definition for definition in registry.definitions_for_family("agent_tasks")}
        call_state_definitions = {definition.driver: definition for definition in registry.definitions_for_family("call_states")}
        worker_registry_definitions = {
            definition.driver: definition for definition in registry.definitions_for_family("worker_registry")
        }
        subagent_task_definitions = {
            definition.driver: definition for definition in registry.definitions_for_family("subagent_tasks")
        }
        session_definitions = {
            definition.driver: definition for definition in registry.definitions_for_family("voicebot_sessions")
        }
        transcript_definitions = {
            definition.driver: definition for definition in registry.definitions_for_family("transcripts")
        }
        artifact_definitions = {
            definition.driver: definition for definition in registry.definitions_for_family("audio_artifacts")
        }
        sip_trunk_definitions = {
            definition.driver: definition for definition in registry.definitions_for_family("sip_trunks")
        }
        queue_drivers = {definition.driver for definition in registry.definitions_for_family("worker_queue")}

        self.assertIn("jsonl", event_definitions)
        self.assertIn("sqlite", event_definitions)
        self.assertIn("postgres", event_definitions)
        self.assertIn("flowhunt_db", event_definitions)
        self.assertTrue(event_definitions["jsonl"].implemented)
        self.assertTrue(event_definitions["sqlite"].implemented)
        self.assertFalse(event_definitions["postgres"].implemented)
        self.assertFalse(event_definitions["flowhunt_db"].implemented)
        self.assertTrue(agent_task_definitions["redis"].implemented)
        self.assertTrue(call_state_definitions["redis"].implemented)
        self.assertTrue({definition.driver: definition for definition in registry.definitions_for_family("worker_queue")}["redis"].implemented)
        self.assertTrue(worker_registry_definitions["redis"].implemented)
        self.assertFalse(worker_registry_definitions["flowhunt_db"].implemented)
        self.assertTrue(subagent_task_definitions["redis"].implemented)
        self.assertFalse(subagent_task_definitions["flowhunt_db"].implemented)
        self.assertTrue(session_definitions["sqlite"].implemented)
        self.assertTrue(transcript_definitions["sqlite"].implemented)
        self.assertTrue(sip_trunk_definitions["sqlite"].implemented)
        self.assertTrue(artifact_definitions["s3"].implemented)
        self.assertTrue(artifact_definitions["object_storage"].implemented)
        self.assertIn("redis", {definition.driver for definition in registry.definitions_for_family("session_leases")})
        self.assertIn("redis_streams", queue_drivers)
        self.assertIn("flowhunt_queue", queue_drivers)
        provider_config_definitions = {
            definition.driver: definition for definition in registry.definitions_for_family("provider_config")
        }
        self.assertTrue(provider_config_definitions["sqlite"].implemented)

    def test_registry_payload_marks_planned_drivers_as_not_implemented(self) -> None:
        payload = storage_drivers_payload(Settings())

        events = {
            definition["driver"]: definition
            for definition in payload["registry"]["families"]["events"]
        }
        queues = {
            definition["driver"]: definition
            for definition in payload["registry"]["families"]["worker_queue"]
        }

        self.assertTrue(events["jsonl"]["implemented"])
        self.assertEqual(events["jsonl"]["status"], "implemented")
        self.assertTrue(events["jsonl"]["selectable"])
        self.assertEqual(events["jsonl"]["unavailable_reason"], "")
        self.assertTrue(events["sqlite"]["implemented"])
        self.assertFalse(events["postgres"]["implemented"])
        self.assertEqual(events["postgres"]["status"], "planned")
        self.assertFalse(events["postgres"]["selectable"])
        self.assertEqual(events["postgres"]["unavailable_reason"], "planned_not_implemented")
        self.assertFalse(queues["redis_streams"]["implemented"])
        self.assertEqual(queues["redis_streams"]["status"], "planned")

    def test_implemented_external_drivers_report_runtime_dependencies(self) -> None:
        payload = storage_drivers_payload(Settings())

        session_leases = {
            definition["driver"]: definition
            for definition in payload["registry"]["families"]["session_leases"]
        }
        artifacts = {
            definition["driver"]: definition
            for definition in payload["registry"]["families"]["audio_artifacts"]
        }

        self.assertEqual(session_leases["redis"]["runtime_dependencies"], ["redis"])
        self.assertEqual(artifacts["s3"]["runtime_dependencies"], ["boto3"])
        self.assertEqual(artifacts["object_storage"]["runtime_dependencies"], ["boto3"])
        self.assertTrue(artifacts["s3"]["selectable"])
        self.assertTrue(artifacts["object_storage"]["selectable"])

    def test_planned_driver_selection_is_visible_but_not_buildable(self) -> None:
        settings = Settings(agent_task_store_provider="flowhunt_db")

        payload = storage_drivers_payload(settings)

        self.assertEqual(payload["selected"]["agent_tasks"]["driver"], "flowhunt_db")
        self.assertFalse(payload["selected"]["agent_tasks"]["definition"]["implemented"])
        with self.assertRaisesRegex(ValueError, "Planned drivers not yet selectable"):
            build_agent_task_tracker(settings)

    def test_driver_aliases_are_normalized(self) -> None:
        self.assertEqual(normalize_driver_name("in-memory"), "memory")
        self.assertEqual(normalize_driver_name("fs"), "filesystem")

    def test_selected_storage_drivers_cover_all_required_families(self) -> None:
        selections = selected_storage_drivers(Settings())

        self.assertEqual(
            set(selections),
            {
                "events",
                "transcripts",
                "voicebot_sessions",
                "session_leases",
                "agent_tasks",
                "worker_queue",
                "worker_registry",
                "call_states",
                "provider_config",
                "sip_trunks",
                "subagent_tasks",
                "audio_artifacts",
            },
        )
        self.assertEqual(selections["events"].driver, "jsonl")
        self.assertEqual(selections["transcripts"].driver, "jsonl")
        self.assertEqual(selections["audio_artifacts"].driver, "filesystem")

    def test_local_store_builders_attach_driver_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                event_store_path=f"{directory}/events.jsonl",
                transcript_dir=f"{directory}/transcripts",
                provider_config_store_path=f"{directory}/provider_config.json",
                agent_task_store_path=f"{directory}/agent_tasks.json",
                subagent_task_store_path=f"{directory}/subagent_tasks.json",
                tts_cache_dir=f"{directory}/tts-cache",
            )
            transcripts = build_transcript_store(settings)
            events = build_event_store(settings, transcripts)
            agent_tasks = build_agent_task_tracker(settings)
            provider_configs = build_provider_config_store(settings)
            subagent_tasks = build_subagent_task_store(settings)
            artifacts = build_audio_artifact_store(settings)

        self.assertEqual(attached_storage_driver(transcripts).family, "transcripts")
        self.assertEqual(attached_storage_driver(events).driver, "jsonl")
        self.assertEqual(attached_storage_driver(provider_configs).family, "provider_config")
        self.assertEqual(attached_storage_driver(agent_tasks).family, "agent_tasks")
        self.assertEqual(attached_storage_driver(subagent_tasks).family, "subagent_tasks")
        self.assertEqual(attached_storage_driver(artifacts).family, "audio_artifacts")

    def test_jsonl_alias_still_selects_json_for_object_stores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tracker = build_agent_task_tracker(
                Settings(agent_task_store_provider="jsonl", agent_task_store_path=f"{directory}/agent_tasks.json")
            )

        self.assertEqual(attached_storage_driver(tracker).driver, "json")
        self.assertEqual(attached_storage_driver(tracker).configured_driver, "jsonl")

    def test_sqlite_event_store_satisfies_event_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            assert_event_store_contract(
                self,
                lambda: SQLiteEventStore(f"sqlite:///{directory}/events.sqlite3", max_context_events=20),
            )

    def test_sqlite_event_store_can_close_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteEventStore(f"sqlite:///{directory}/events.sqlite3", max_context_events=20)
            store.append("call-1", "call_started", {})

            store.close()

            with self.assertRaises(sqlite3.ProgrammingError):
                store.list_events()

    def test_redis_session_lease_store_uses_client_boundary(self) -> None:
        store = RedisSessionLeaseStore("redis://test", client=FakeRedis())

        lease = store.acquire("ws-1", "bot-1", "session-1", "owner-1", 30)

        self.assertIsNotNone(lease)
        self.assertIsNotNone(store.get("ws-1", "bot-1", "session-1"))
        self.assertIsNone(store.acquire("ws-1", "bot-1", "session-1", "owner-2", 30))
        self.assertEqual(store.release("ws-1", "bot-1", "session-1", owner="owner-1").owner, "owner-1")

    def test_family_level_driver_settings_select_managed_targets(self) -> None:
        settings = Settings(
            event_store_provider="sqlite",
            session_lease_store_provider="redis",
            relational_database_url="sqlite:////data/voicebot.sqlite3",
            redis_url="redis://redis:6379/0",
        )

        payload = storage_drivers_payload(settings)

        self.assertEqual(payload["selected"]["events"]["driver"], "sqlite")
        self.assertEqual(payload["selected"]["session_leases"]["driver"], "redis")
        self.assertEqual(payload["selected"]["events"]["options"]["database_url"]["redacted"], True)
        self.assertEqual(payload["selected"]["session_leases"]["options"]["redis_url"]["redacted"], True)

    def test_agent_task_redis_driver_is_buildable(self) -> None:
        with patch("voicebot.storage.redis_agent_tasks._redis_client_from_url", return_value=FakeRedis()):
            tracker = build_agent_task_tracker(
                Settings(agent_task_store_provider="redis", redis_url="redis://test")
            )

        self.assertEqual(attached_storage_driver(tracker).driver, "redis")
        self.assertEqual(attached_storage_driver(tracker).options["redis_url"], "redis://test")

    def test_call_state_redis_driver_is_buildable(self) -> None:
        with patch("voicebot.storage.redis_call_state._redis_client_from_url", return_value=FakeRedis()):
            store = build_call_state_store(
                Settings(call_state_store_provider="redis", redis_url="redis://test")
            )

        self.assertEqual(attached_storage_driver(store).driver, "redis")
        self.assertEqual(attached_storage_driver(store).options["redis_url"], "redis://test")

    def test_worker_registry_redis_driver_is_buildable(self) -> None:
        with patch("voicebot.storage.redis_worker_registry._redis_client_from_url", return_value=FakeRedis()):
            registry = build_worker_registry(
                Settings(worker_registry_store_provider="redis", redis_url="redis://test")
            )

        self.assertIsInstance(registry, RedisWorkerRegistry)
        self.assertEqual(attached_storage_driver(registry).driver, "redis")
        self.assertEqual(attached_storage_driver(registry).options["redis_url"], "redis://test")

    def test_worker_queue_redis_driver_is_buildable(self) -> None:
        with patch("voicebot.storage.redis_worker_queue._redis_client_from_url", return_value=FakeRedis()):
            store = build_worker_queue_store(
                Settings(worker_queue_store_provider="redis", redis_url="redis://test")
            )

        self.assertIsInstance(store, RedisWorkerQueueStore)
        self.assertEqual(attached_storage_driver(store).driver, "redis")
        self.assertEqual(attached_storage_driver(store).options["redis_url"], "redis://test")

    def test_subagent_task_redis_driver_is_buildable(self) -> None:
        with patch("voicebot.storage.redis_subagent_tasks._redis_client_from_url", return_value=FakeRedis()):
            store = build_subagent_task_store(
                Settings(subagent_task_store_provider="redis", redis_url="redis://test")
            )

        self.assertIsInstance(store, RedisSubagentTaskStore)
        self.assertEqual(attached_storage_driver(store).driver, "redis")
        self.assertEqual(attached_storage_driver(store).options["redis_url"], "redis://test")

    def test_s3_audio_artifact_driver_is_buildable(self) -> None:
        with patch("voicebot.storage.artifacts._s3_client", return_value=FakeS3()):
            store = build_audio_artifact_store(
                Settings(
                    audio_artifact_store_provider="s3",
                    object_storage_bucket="voicebot-audio",
                    object_storage_endpoint="https://s3.example.com",
                    object_storage_region="eu-central-1",
                )
            )

        selection = attached_storage_driver(store)
        self.assertIsInstance(store, S3ArtifactStore)
        self.assertEqual(selection.driver, "s3")
        self.assertEqual(selection.options["bucket"], "voicebot-audio")
        self.assertEqual(selection.options["endpoint"], "https://s3.example.com")
        self.assertEqual(selection.options["region"], "eu-central-1")

    def test_object_storage_audio_artifact_driver_is_buildable(self) -> None:
        with patch("voicebot.storage.artifacts._s3_client", return_value=FakeS3()):
            store = build_audio_artifact_store(
                Settings(
                    audio_artifact_store_provider="object_storage",
                    object_storage_bucket="voicebot-audio",
                    object_storage_endpoint="https://object-storage.example.com",
                    object_storage_region="eu-central-1",
                )
            )

        selection = attached_storage_driver(store)
        self.assertIsInstance(store, S3ArtifactStore)
        self.assertEqual(selection.driver, "object_storage")
        self.assertEqual(selection.configured_driver, "object_storage")
        self.assertEqual(selection.options["bucket"], "voicebot-audio")
        self.assertEqual(selection.options["endpoint"], "https://object-storage.example.com")

    def test_s3_audio_artifact_selection_reports_object_storage_options(self) -> None:
        payload = storage_drivers_payload(
            Settings(
                audio_artifact_store_provider="s3",
                object_storage_bucket="voicebot-audio",
                object_storage_endpoint="https://s3.example.com",
                object_storage_region="eu-central-1",
            )
        )

        selected = payload["selected"]["audio_artifacts"]
        self.assertEqual(selected["driver"], "s3")
        self.assertTrue(selected["definition"]["implemented"])
        self.assertEqual(selected["options"]["bucket"], "voicebot-audio")
        self.assertEqual(selected["options"]["endpoint"], "https://s3.example.com")
        self.assertEqual(selected["options"]["region"], "eu-central-1")

    def test_provider_config_sqlite_driver_is_buildable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = build_provider_config_store(
                Settings(
                    provider_config_store_provider="sqlite",
                    relational_database_url=f"sqlite:///{directory}/provider_config.sqlite3",
                )
            )

            self.assertIsInstance(store, SQLiteProviderConfigStore)
            self.assertEqual(attached_storage_driver(store).driver, "sqlite")
            self.assertEqual(attached_storage_driver(store).options["database_url"].startswith("sqlite:///"), True)
            store.close()

    def test_voicebot_session_sqlite_driver_is_buildable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = build_voicebot_session_store(
                Settings(
                    voicebot_session_store_provider="sqlite",
                    relational_database_url=f"sqlite:///{directory}/sessions.sqlite3",
                )
            )

            self.assertIsInstance(store, SQLiteVoicebotSessionStore)
            self.assertEqual(attached_storage_driver(store).driver, "sqlite")
            self.assertEqual(attached_storage_driver(store).options["database_url"].startswith("sqlite:///"), True)
            store.close()

    def test_transcript_sqlite_driver_is_buildable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = build_transcript_store(
                Settings(
                    transcript_store_provider="sqlite",
                    relational_database_url=f"sqlite:///{directory}/transcripts.sqlite3",
                )
            )

            self.assertIsInstance(store, SQLiteTranscriptStore)
            self.assertEqual(attached_storage_driver(store).driver, "sqlite")
            self.assertEqual(attached_storage_driver(store).options["database_url"].startswith("sqlite:///"), True)
            store.close()

    def test_sip_trunk_sqlite_driver_is_buildable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = build_sip_trunk_store(
                Settings(
                    sip_trunk_store_provider="sqlite",
                    relational_database_url=f"sqlite:///{directory}/sip_trunks.sqlite3",
                    sip_trunk_pjsip_include_path=f"{directory}/pjsip-trunks.conf",
                )
            )

            self.assertIsInstance(store, SQLiteSipTrunkStore)
            self.assertEqual(attached_storage_driver(store).driver, "sqlite")
            self.assertEqual(attached_storage_driver(store).options["database_url"].startswith("sqlite:///"), True)
            store.close()

    def test_unknown_storage_driver_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported storage driver"):
            storage_drivers_payload(Settings(event_store_provider="unknown"))

    def test_storage_drivers_endpoint_exposes_selected_drivers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                EventStore(max_context_events=20),
                CallRegistry(),
                AgentTaskTracker(),
                WebSocketHub(),
                TranscriptStore(directory),
                None,
            )
            response = TestClient(app).get("/storage/drivers")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("events", payload["registry"]["families"])
        self.assertEqual(payload["selected"]["events"]["driver"], "jsonl")
        self.assertEqual(payload["selected"]["provider_config"]["driver"], "json")


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


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, Metadata: dict[str, str]) -> dict:
        self.objects[(Bucket, Key)] = (Body, Metadata)
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        try:
            data, metadata = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise FileNotFoundError(Key) from exc
        return {"Body": FakeBody(data), "Metadata": metadata}

    def delete_object(self, *, Bucket: str, Key: str) -> dict:
        self.objects.pop((Bucket, Key), None)
        return {}

    def list_objects_v2(self, *, Bucket: str, Prefix: str) -> dict:
        contents = [
            {"Key": key}
            for bucket, key in sorted(self.objects)
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return {"Contents": contents}

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        try:
            _data, metadata = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise FileNotFoundError(Key) from exc
        return {"Metadata": metadata}


if __name__ == "__main__":
    unittest.main()
