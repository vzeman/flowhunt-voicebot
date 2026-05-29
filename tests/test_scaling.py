from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.scaling import (
    RoutingKey,
    JsonWorkerQueueStore,
    JsonWorkerRegistry,
    WorkerInstance,
    WorkerQueueEnvelope,
    WorkerQueueStore,
    WorkerRegistry,
    WorkloadProfile,
    WorkspaceBackpressure,
    build_workload_plan,
    default_deployment_topology,
)
from voicebot.config import Settings
from voicebot.runtime_storage import build_worker_registry
from voicebot.transcripts import TranscriptStore


class ScalingTests(unittest.TestCase):
    def test_routing_key_partitions_by_workspace_voicebot_and_session(self) -> None:
        key = RoutingKey("workspace-1", "voicebot-1", session_id="call-1", provider="openai")

        self.assertEqual(key.partition_key(), "workspace-1:voicebot-1:call-1")
        self.assertEqual(key.provider_key(), "workspace-1:voicebot-1:openai")

    def test_default_topology_defines_core_worker_queues(self) -> None:
        topology = default_deployment_topology()

        self.assertEqual(topology.queue_for_role("media_ingress").queue, "voicebot.media")
        self.assertEqual(topology.queue_for_role("stt_worker").max_inflight_per_provider, 50)
        self.assertEqual(topology.queue_for_role("agent_worker").concurrency, 16)
        self.assertIn("redis", topology.shared_state)

    def test_topology_serializes_to_api_friendly_shape(self) -> None:
        data = default_deployment_topology().as_dict()

        self.assertEqual(data["event_bus"], "workspace_event_stream")
        self.assertIn("flowhunt_db", data["shared_state"])
        self.assertIn("media_ingress", {queue["role"] for queue in data["queues"]})

    def test_backpressure_allows_up_to_limit_and_releases(self) -> None:
        limiter = WorkspaceBackpressure(max_inflight=2)

        self.assertTrue(limiter.acquire("workspace-1"))
        self.assertTrue(limiter.acquire("workspace-1"))
        self.assertFalse(limiter.acquire("workspace-1"))
        limiter.release("workspace-1")
        self.assertTrue(limiter.acquire("workspace-1"))

    def test_backpressure_rejects_invalid_limits_and_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_inflight"):
            WorkspaceBackpressure(max_inflight=0)
        limiter = WorkspaceBackpressure(max_inflight=1)
        with self.assertRaisesRegex(ValueError, "backpressure key"):
            limiter.acquire(" ")
        with self.assertRaisesRegex(ValueError, "backpressure key"):
            limiter.release("")

    def test_worker_queue_envelope_serializes_routing_and_payload(self) -> None:
        envelope = WorkerQueueEnvelope(
            item_id="item-1",
            kind="stt_turn",
            routing=RoutingKey("workspace-1", "voicebot-1", session_id="session-1", provider="openai"),
            queue="voicebot.stt",
            payload={"event_id": 42},
            trace_id="trace-1",
            created_at="2026-05-28T00:00:00+00:00",
            attempt=1,
        )

        data = envelope.as_dict()

        self.assertEqual(envelope.partition_key(), "workspace-1:voicebot-1:session-1")
        self.assertEqual(data["routing"]["provider_key"], "workspace-1:voicebot-1:openai")
        self.assertEqual(data["payload"], {"event_id": 42})
        self.assertEqual(data["attempt"], 1)

    def test_worker_queue_envelope_rejects_invalid_records(self) -> None:
        valid = {
            "item_id": "item-1",
            "kind": "agent_turn",
            "routing": RoutingKey("workspace-1", "voicebot-1"),
            "queue": "voicebot.agent",
        }

        invalid_cases = [
            ({**valid, "item_id": " "}, "item_id"),
            ({**valid, "kind": "unknown"}, "unsupported work item kind"),
            ({**valid, "queue": ""}, "queue"),
            ({**valid, "created_at": "not-a-time"}, "Invalid isoformat"),
            ({**valid, "attempt": -1}, "attempt"),
        ]

        for kwargs, message in invalid_cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    WorkerQueueEnvelope(**kwargs)

    def test_worker_queue_store_claims_acks_and_releases_items(self) -> None:
        store = WorkerQueueStore()
        first = WorkerQueueEnvelope(
            item_id="item-1",
            kind="stt_turn",
            routing=RoutingKey("workspace-1", "voicebot-1", session_id="session-1"),
            queue="voicebot.stt",
        )
        second = WorkerQueueEnvelope(
            item_id="item-2",
            kind="stt_turn",
            routing=RoutingKey("workspace-1", "voicebot-1", session_id="session-1"),
            queue="voicebot.stt",
        )
        store.enqueue(first)
        store.enqueue(second)

        claimed = store.claim("voicebot.stt", "worker-1", limit=1)
        wrong_ack = store.ack("item-1", owner="worker-2")
        released = store.release("item-1", owner="worker-1")
        reclaimed = store.claim("voicebot.stt", "worker-1", limit=2)
        acked = store.ack("item-1", owner="worker-1")

        self.assertEqual([item.item_id for item in claimed], ["item-1"])
        self.assertEqual(claimed[0].attempt, 1)
        self.assertIsNone(wrong_ack)
        self.assertEqual(released.item_id if released else None, "item-1")
        self.assertEqual([item.item_id for item in reclaimed], ["item-1", "item-2"])
        self.assertEqual(reclaimed[0].attempt, 2)
        self.assertEqual(acked.item_id if acked else None, "item-1")
        self.assertEqual([claim["item"]["item_id"] for claim in store.claimed()], ["item-2"])

    def test_worker_queue_store_expires_claims_back_to_pending(self) -> None:
        store = WorkerQueueStore()
        now = datetime(2026, 5, 28, tzinfo=UTC)
        envelope = WorkerQueueEnvelope(
            item_id="item-1",
            kind="agent_turn",
            routing=RoutingKey("workspace-1", "voicebot-1"),
            queue="voicebot.agent",
            created_at=now.isoformat(),
        )
        store.enqueue(envelope)
        store.claim("voicebot.agent", "worker-1", ttl_seconds=1.0, now=now)

        expired = store.expire(now + timedelta(seconds=2))

        self.assertEqual([item.item_id for item in expired], ["item-1"])
        self.assertEqual(store.claimed(now=now + timedelta(seconds=2)), ())
        self.assertEqual([item.item_id for item in store.pending("voicebot.agent")], ["item-1"])

    def test_worker_queue_store_rejects_duplicate_and_invalid_claim_requests(self) -> None:
        store = WorkerQueueStore()
        envelope = WorkerQueueEnvelope(
            item_id="item-1",
            kind="tts_request",
            routing=RoutingKey("workspace-1", "voicebot-1"),
            queue="voicebot.tts",
        )
        store.enqueue(envelope)

        with self.assertRaisesRegex(ValueError, "already exists"):
            store.enqueue(envelope)
        with self.assertRaisesRegex(ValueError, "queue"):
            store.claim(" ", "worker-1")
        with self.assertRaisesRegex(ValueError, "owner"):
            store.claim("voicebot.tts", " ")
        with self.assertRaisesRegex(ValueError, "limit"):
            store.claim("voicebot.tts", "worker-1", limit=0)
        with self.assertRaisesRegex(ValueError, "ttl_seconds"):
            store.claim("voicebot.tts", "worker-1", ttl_seconds=0)

    def test_worker_queue_store_snapshot_groups_pending_and_claimed(self) -> None:
        store = WorkerQueueStore()
        now = datetime(2026, 5, 28, tzinfo=UTC)
        store.enqueue(
            WorkerQueueEnvelope(
                item_id="item-1",
                kind="session_event",
                routing=RoutingKey("workspace-1", "voicebot-1"),
                queue="voicebot.sessions",
                created_at=now.isoformat(),
            )
        )
        store.enqueue(
            WorkerQueueEnvelope(
                item_id="item-2",
                kind="session_event",
                routing=RoutingKey("workspace-1", "voicebot-1"),
                queue="voicebot.sessions",
                created_at=now.isoformat(),
            )
        )
        store.claim("voicebot.sessions", "worker-1", now=now)

        snapshot = store.snapshot(now=now)

        self.assertEqual([item["item_id"] for item in snapshot["pending"]["voicebot.sessions"]], ["item-2"])
        self.assertEqual(snapshot["claimed"][0]["item"]["item_id"], "item-1")
        self.assertEqual(snapshot["claimed"][0]["owner"], "worker-1")

    def test_json_worker_queue_store_persists_pending_and_claimed_items(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/queue.json"
            now = datetime.now(UTC)
            first = JsonWorkerQueueStore(path)
            first.enqueue(
                WorkerQueueEnvelope(
                    item_id="pending-1",
                    kind="agent_turn",
                    routing=RoutingKey("workspace-1", "voicebot-1"),
                    queue="voicebot.agent",
                    created_at=now.isoformat(),
                )
            )
            first.enqueue(
                WorkerQueueEnvelope(
                    item_id="claimed-1",
                    kind="agent_turn",
                    routing=RoutingKey("workspace-1", "voicebot-1"),
                    queue="voicebot.agent",
                    created_at=now.isoformat(),
                )
            )
            first.claim("voicebot.agent", "worker-1", limit=1, ttl_seconds=60, now=now)

            reloaded = JsonWorkerQueueStore(path)

        self.assertEqual([item.item_id for item in reloaded.pending("voicebot.agent")], ["claimed-1"])
        self.assertEqual([claim["item"]["item_id"] for claim in reloaded.claimed(now=now)], ["pending-1"])
        self.assertEqual(reloaded.load_diagnostics["loaded_pending"], 1)
        self.assertEqual(reloaded.load_diagnostics["loaded_claimed"], 1)

    def test_json_worker_queue_store_requeues_expired_claims_on_reload(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/queue.json"
            first = JsonWorkerQueueStore(path)
            first.enqueue(
                WorkerQueueEnvelope(
                    item_id="item-1",
                    kind="agent_turn",
                    routing=RoutingKey("workspace-1", "voicebot-1"),
                    queue="voicebot.agent",
                )
            )
            first.claim("voicebot.agent", "worker-1", ttl_seconds=0.1)

            import time

            time.sleep(0.12)
            reloaded = JsonWorkerQueueStore(path)

        self.assertEqual([item.item_id for item in reloaded.pending("voicebot.agent")], ["item-1"])
        self.assertEqual(reloaded.claimed(), ())
        self.assertEqual(reloaded.load_diagnostics["requeued_expired_claims"], 1)

    def test_workload_plan_includes_partition_provider_keys_and_capacity_flags(self) -> None:
        plan = build_workload_plan(
            WorkloadProfile(
                "workspace-1",
                "voicebot-1",
                120,
                session_id="session-1",
                stt_provider="openai",
                tts_provider="openai",
                agent_provider="anthropic",
            )
        )

        self.assertEqual(plan["routing"]["partition_key"], "workspace-1:voicebot-1:session-1")
        stt_queue = next(queue for queue in plan["queues"] if queue["role"] == "stt_worker")
        agent_queue = next(queue for queue in plan["queues"] if queue["role"] == "agent_worker")
        self.assertEqual(stt_queue["provider_key"], "workspace-1:voicebot-1:openai")
        self.assertFalse(stt_queue["workspace_capacity_ok"])
        self.assertEqual(agent_queue["provider_key"], "workspace-1:voicebot-1:anthropic")

    def test_worker_registry_tracks_active_workers_by_role_and_workspace(self) -> None:
        registry = WorkerRegistry(heartbeat_ttl_seconds=30)
        now = datetime(2026, 5, 28, tzinfo=UTC)
        registry.heartbeat(WorkerInstance("media-1", "media_ingress", "voicebot.media", workspace_id="workspace-1"), now)
        registry.heartbeat(WorkerInstance("stt-1", "stt_worker", "voicebot.stt"), now)

        active_media = registry.active(role="media_ingress", workspace_id="workspace-1", now=now)

        self.assertEqual([worker.worker_id for worker in active_media], ["media-1"])
        self.assertEqual(len(registry.active(workspace_id="workspace-2", now=now)), 1)

    def test_worker_instance_rejects_invalid_presence_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "worker_id"):
            WorkerInstance("", "stt_worker", "voicebot.stt")
        with self.assertRaisesRegex(ValueError, "queue"):
            WorkerInstance("stt-1", "stt_worker", "")
        with self.assertRaisesRegex(ValueError, "capacity"):
            WorkerInstance("stt-1", "stt_worker", "voicebot.stt", capacity=0)

    def test_worker_registry_expires_stale_workers_and_marks_draining(self) -> None:
        registry = WorkerRegistry(heartbeat_ttl_seconds=10)
        now = datetime(2026, 5, 28, tzinfo=UTC)
        registry.heartbeat(WorkerInstance("agent-1", "agent_worker", "voicebot.agent"), now)
        draining = registry.mark_draining("agent-1", now + timedelta(seconds=1))

        self.assertEqual(draining.status, "draining")
        self.assertEqual(registry.active(now=now + timedelta(seconds=2)), ())
        expired = registry.expire(now + timedelta(seconds=12))

        self.assertEqual([worker.worker_id for worker in expired], ["agent-1"])
        self.assertEqual(registry.snapshot(now + timedelta(seconds=12))["workers"], [])

    def test_worker_registry_rejects_invalid_heartbeat_ttl(self) -> None:
        with self.assertRaisesRegex(ValueError, "heartbeat_ttl_seconds"):
            WorkerRegistry(heartbeat_ttl_seconds=0)
        with self.assertRaisesRegex(ValueError, "heartbeat_ttl_seconds"):
            WorkerRegistry(heartbeat_ttl_seconds=-1)

    def test_worker_registry_rejects_role_or_queue_moves(self) -> None:
        registry = WorkerRegistry(heartbeat_ttl_seconds=30)
        now = datetime(2026, 5, 28, tzinfo=UTC)
        registry.heartbeat(WorkerInstance("worker-1", "agent_worker", "voicebot.agent"), now)

        with self.assertRaisesRegex(ValueError, "roles"):
            registry.heartbeat(WorkerInstance("worker-1", "stt_worker", "voicebot.stt"), now)
        with self.assertRaisesRegex(ValueError, "queues"):
            registry.heartbeat(WorkerInstance("worker-1", "agent_worker", "voicebot.other"), now)

    def test_worker_registry_reports_active_capacity_by_role(self) -> None:
        registry = WorkerRegistry(heartbeat_ttl_seconds=30)
        now = datetime(2026, 5, 28, tzinfo=UTC)
        registry.heartbeat(
            WorkerInstance("stt-global", "stt_worker", "voicebot.stt", capacity=5),
            now,
        )
        registry.heartbeat(
            WorkerInstance("stt-workspace", "stt_worker", "voicebot.stt", workspace_id="workspace-1", capacity=3),
            now,
        )
        registry.heartbeat(
            WorkerInstance("agent-workspace", "agent_worker", "voicebot.agent", workspace_id="workspace-1", capacity=2),
            now,
        )
        registry.heartbeat(
            WorkerInstance("tts-other", "tts_worker", "voicebot.tts", workspace_id="workspace-2", capacity=9),
            now,
        )

        summary = registry.capacity_summary(workspace_id="workspace-1", now=now)

        self.assertEqual(summary["total_workers"], 3)
        self.assertEqual(summary["total_capacity"], 10)
        self.assertEqual(summary["roles"]["stt_worker"], {"workers": 2, "capacity": 8})
        self.assertEqual(summary["roles"]["agent_worker"], {"workers": 1, "capacity": 2})
        self.assertNotIn("tts_worker", summary["roles"])

    def test_worker_registry_filters_capacity_by_voicebot_affinity(self) -> None:
        registry = WorkerRegistry(heartbeat_ttl_seconds=30)
        now = datetime(2026, 5, 28, tzinfo=UTC)
        registry.heartbeat(WorkerInstance("agent-global", "agent_worker", "voicebot.agent", capacity=4), now)
        registry.heartbeat(
            WorkerInstance(
                "agent-voicebot-1",
                "agent_worker",
                "voicebot.agent",
                workspace_id="workspace-1",
                voicebot_id="voicebot-1",
                capacity=2,
            ),
            now,
        )
        registry.heartbeat(
            WorkerInstance(
                "agent-voicebot-2",
                "agent_worker",
                "voicebot.agent",
                workspace_id="workspace-1",
                voicebot_id="voicebot-2",
                capacity=8,
            ),
            now,
        )

        summary = registry.capacity_summary(workspace_id="workspace-1", voicebot_id="voicebot-1", now=now)

        self.assertEqual(summary["voicebot_id"], "voicebot-1")
        self.assertEqual(summary["roles"]["agent_worker"], {"workers": 2, "capacity": 6})

    def test_json_worker_registry_persists_presence_and_drain_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workers.json"
            now = datetime.now(UTC)
            registry = JsonWorkerRegistry(path, heartbeat_ttl_seconds=60)
            registry.heartbeat(WorkerInstance("agent-1", "agent_worker", "voicebot.agent", capacity=2), now)
            registry.mark_draining("agent-1", now + timedelta(seconds=1))

            reloaded = JsonWorkerRegistry(path, heartbeat_ttl_seconds=60)

            self.assertEqual(reloaded.load_diagnostics["loaded_workers"], 1)
            self.assertEqual(reloaded.snapshot(now + timedelta(seconds=2))["workers"][0]["status"], "draining")

    def test_json_worker_registry_skips_invalid_duplicate_and_expired_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workers.json"
            now = datetime.now(UTC)
            expired = (now - timedelta(seconds=120)).isoformat()
            current = now.isoformat()
            path.write_text(
                f"""
                {{
                  "workers": [
                    {{"worker_id": "agent-1", "role": "agent_worker", "queue": "voicebot.agent", "last_heartbeat_at": "{current}"}},
                    {{"worker_id": "agent-1", "role": "agent_worker", "queue": "voicebot.agent", "last_heartbeat_at": "{current}"}},
                    {{"worker_id": "old-1", "role": "agent_worker", "queue": "voicebot.agent", "last_heartbeat_at": "{expired}"}},
                    {{"worker_id": "bad-1", "role": "unknown", "queue": "voicebot.agent", "last_heartbeat_at": "{current}"}}
                  ]
                }}
                """,
                encoding="utf-8",
            )

            registry = JsonWorkerRegistry(path, heartbeat_ttl_seconds=60)

            self.assertEqual([worker.worker_id for worker in registry.active(now=now)], ["agent-1"])
            self.assertEqual(registry.load_diagnostics["loaded_workers"], 1)
            self.assertEqual(registry.load_diagnostics["skipped_duplicate_worker_ids"], 1)
            self.assertEqual(registry.load_diagnostics["skipped_expired_workers"], 1)
            self.assertEqual(registry.load_diagnostics["skipped_invalid_workers"], 1)

    def test_worker_registry_builder_supports_configured_providers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            json_registry = build_worker_registry(
                Settings(worker_registry_store_provider="json", worker_registry_store_path=f"{tmp}/workers.json")
            )
            memory_registry = build_worker_registry(Settings(worker_registry_store_provider="memory"))

        self.assertIsInstance(json_registry, JsonWorkerRegistry)
        self.assertIsInstance(memory_registry, WorkerRegistry)

    def test_scaling_topology_endpoint_returns_runtime_topology(self) -> None:
        client = self.build_client()

        response = client.get("/scaling/topology")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["event_bus"], "workspace_event_stream")
        self.assertIn("voicebot.media", {queue["queue"] for queue in response.json()["queues"]})

    def test_scaling_workload_plan_endpoint_validates_and_plans(self) -> None:
        client = self.build_client()

        response = client.post(
            "/scaling/workload-plan",
            json={
                "workspace_id": "workspace-1",
                "voicebot_id": "voicebot-1",
                "concurrent_sessions": 50,
                "session_id": "session-1",
                "stt_provider": "openai",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["routing"]["partition_key"], "workspace-1:voicebot-1:session-1")

    def test_scaling_worker_presence_api_tracks_capacity_and_drain(self) -> None:
        client = self.build_client()

        heartbeat = client.post(
            "/scaling/workers/heartbeat",
            json={
                "worker_id": "agent-1",
                "role": "agent_worker",
                "queue": "voicebot.agent",
                "workspace_id": "workspace-1",
                "capacity": 3,
            },
        )
        list_response = client.get("/scaling/workers?role=agent_worker&workspace_id=workspace-1")
        voicebot_capacity = client.get("/scaling/capacity?workspace_id=workspace-1&voicebot_id=voicebot-1")
        capacity = client.get("/scaling/capacity?workspace_id=workspace-1")
        drain = client.post("/scaling/workers/agent-1/drain")
        after_drain = client.get("/scaling/workers?role=agent_worker&workspace_id=workspace-1")
        removed = client.delete("/scaling/workers/agent-1")

        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(heartbeat.json()["worker"]["worker_id"], "agent-1")
        self.assertEqual([worker["worker_id"] for worker in list_response.json()["workers"]], ["agent-1"])
        self.assertEqual(voicebot_capacity.json()["voicebot_id"], "voicebot-1")
        self.assertEqual(capacity.json()["roles"]["agent_worker"], {"workers": 1, "capacity": 3})
        self.assertEqual(drain.json()["worker"]["status"], "draining")
        self.assertEqual(after_drain.json()["workers"], [])
        self.assertTrue(removed.json()["removed"])

    def test_scaling_worker_presence_api_rejects_invalid_role(self) -> None:
        client = self.build_client()

        response = client.post(
            "/scaling/workers/heartbeat",
            json={"worker_id": "bad-1", "role": "unknown", "queue": "voicebot.unknown"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("unsupported worker role", response.json()["detail"])

    def test_scaling_queue_api_enqueues_claims_acks_and_releases_work(self) -> None:
        client = self.build_client()

        enqueue = client.post(
            "/scaling/queue/enqueue",
            json={
                "item_id": "item-1",
                "kind": "agent_turn",
                "routing": {"workspace_id": "workspace-1", "voicebot_id": "voicebot-1", "session_id": "session-1"},
                "queue": "voicebot.agent",
                "payload": {"event_id": 42},
            },
        )
        snapshot = client.get("/scaling/queue")
        claim = client.post(
            "/scaling/queue/claim",
            json={"queue": "voicebot.agent", "owner": "worker-1", "ttl_seconds": 30},
        )
        wrong_ack = client.post("/scaling/queue/ack", json={"item_id": "item-1", "owner": "worker-2"})
        release = client.post("/scaling/queue/release", json={"item_id": "item-1", "owner": "worker-1"})
        reclaim = client.post(
            "/scaling/queue/claim",
            json={"queue": "voicebot.agent", "owner": "worker-1", "ttl_seconds": 30},
        )
        ack = client.post("/scaling/queue/ack", json={"item_id": "item-1", "owner": "worker-1"})

        self.assertEqual(enqueue.status_code, 200)
        self.assertEqual(enqueue.json()["item"]["routing"]["partition_key"], "workspace-1:voicebot-1:session-1")
        self.assertEqual(snapshot.json()["pending"]["voicebot.agent"][0]["item_id"], "item-1")
        self.assertEqual([item["item_id"] for item in claim.json()["items"]], ["item-1"])
        self.assertEqual(claim.json()["items"][0]["attempt"], 1)
        self.assertEqual(wrong_ack.status_code, 404)
        self.assertTrue(release.json()["released"])
        self.assertEqual(reclaim.json()["items"][0]["attempt"], 2)
        self.assertTrue(ack.json()["acked"])

    def test_scaling_queue_api_rejects_invalid_enqueue_and_claim_requests(self) -> None:
        client = self.build_client()

        invalid_enqueue = client.post(
            "/scaling/queue/enqueue",
            json={
                "item_id": " ",
                "kind": "agent_turn",
                "routing": {"workspace_id": "workspace-1", "voicebot_id": "voicebot-1"},
                "queue": "voicebot.agent",
            },
        )
        invalid_claim = client.post(
            "/scaling/queue/claim",
            json={"queue": "voicebot.agent", "owner": "worker-1", "limit": 0},
        )

        self.assertEqual(invalid_enqueue.status_code, 400)
        self.assertIn("item_id", invalid_enqueue.json()["detail"])
        self.assertEqual(invalid_claim.status_code, 400)
        self.assertIn("limit", invalid_claim.json()["detail"])

    def build_client(self) -> TestClient:
        app = create_app(
            EventStore(max_context_events=20),
            CallRegistry(),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        return TestClient(app)


if __name__ == "__main__":
    unittest.main()
