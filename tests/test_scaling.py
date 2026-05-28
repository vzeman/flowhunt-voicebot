from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.scaling import (
    RoutingKey,
    WorkerInstance,
    WorkerRegistry,
    WorkloadProfile,
    WorkspaceBackpressure,
    build_workload_plan,
    default_deployment_topology,
)
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
