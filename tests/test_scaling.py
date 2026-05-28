from __future__ import annotations

import unittest

from voicebot.scaling import RoutingKey, WorkspaceBackpressure, default_deployment_topology


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


if __name__ == "__main__":
    unittest.main()
