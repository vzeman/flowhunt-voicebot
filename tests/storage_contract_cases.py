from __future__ import annotations

import time
from typing import Any, Callable

from voicebot.scaling import WorkerInstance


def assert_event_store_contract(testcase: Any, factory: Callable[[], Any]) -> None:
    store = factory()

    first = store.append("call-1", "call_started", {"workspace_id": "ws-1", "voicebot_id": "bot-1"})
    second = store.append("call-1", "user_transcript", {"workspace_id": "ws-1", "voicebot_id": "bot-1", "text": "hello"})
    store.append("call-2", "call_started", {"workspace_id": "ws-2", "voicebot_id": "bot-2"})

    testcase.assertEqual(second.id, first.id + 1)
    testcase.assertEqual(store.get_event(first.id).call_id, "call-1")
    testcase.assertEqual([event.id for event in store.list_events(after=first.id)], [second.id, second.id + 1])
    testcase.assertEqual([event.call_id for event in store.list_events(call_id="call-1")], ["call-1", "call-1"])
    testcase.assertEqual(
        [event.call_id for event in store.list_events(workspace_id="ws-1", voicebot_id="bot-1")],
        ["call-1", "call-1"],
    )


def assert_call_state_store_contract(testcase: Any, factory: Callable[[], Any]) -> None:
    store = factory()

    active = store.upsert({"call_id": "call-1", "workspace_id": "ws-1", "playback_active": True})
    ended = store.end("call-1")

    testcase.assertEqual(active["state"], "active")
    testcase.assertEqual(ended["state"], "ended")
    testcase.assertEqual(store.get("call-1")["state"], "ended")
    testcase.assertEqual(store.list(active_only=True), ())

    store.upsert({"call_id": "call-2", "workspace_id": "ws-1", "playback_active": False})
    testcase.assertEqual([item["call_id"] for item in store.list()], ["call-1", "call-2"])
    testcase.assertEqual([item["call_id"] for item in store.list(active_only=True)], ["call-2"])


def assert_agent_task_store_contract(testcase: Any, factory: Callable[[], Any]) -> None:
    store = factory()

    testcase.assertEqual(store.claim([1, 2], "worker-1", 30), [1, 2])
    testcase.assertFalse(store.is_pending(1))
    testcase.assertEqual(store.release_many([1], owner="worker-2"), [])
    testcase.assertFalse(store.is_pending(1))
    testcase.assertEqual(store.release_many([1], owner="worker-1"), [1])
    testcase.assertTrue(store.is_pending(1))

    store.mark_responded(2)
    testcase.assertEqual(store.claim([2], "worker-2", 30), [])
    testcase.assertEqual(store.task_state(2), {"state": "responded"})

    testcase.assertEqual(store.claim([3], "worker-1", 0.1), [3])
    time.sleep(0.12)
    testcase.assertTrue(store.is_pending(3))


def assert_worker_registry_contract(testcase: Any, factory: Callable[[], Any]) -> None:
    store = factory()

    first = store.heartbeat(WorkerInstance("agent-1", "agent_worker", "voicebot.agent", workspace_id="ws-1", capacity=2))
    store.heartbeat(WorkerInstance("agent-2", "agent_worker", "voicebot.agent", workspace_id="ws-2", capacity=3))
    store.heartbeat(WorkerInstance("stt-1", "stt_worker", "voicebot.stt", capacity=4))

    testcase.assertEqual(first.worker_id, "agent-1")
    testcase.assertEqual([worker.worker_id for worker in store.active(role="agent_worker", workspace_id="ws-1")], ["agent-1"])
    testcase.assertEqual(store.capacity_summary(workspace_id="ws-1")["total_capacity"], 6)

    drained = store.mark_draining("agent-1")
    testcase.assertEqual(drained.status, "draining")
    testcase.assertEqual([worker.worker_id for worker in store.active(role="agent_worker")], ["agent-2"])

    testcase.assertTrue(store.remove("agent-1"))
    testcase.assertFalse(store.remove("agent-1"))
    testcase.assertEqual([worker["worker_id"] for worker in store.snapshot()["workers"]], ["agent-2", "stt-1"])


def assert_artifact_store_contract(testcase: Any, factory: Callable[[], Any]) -> None:
    store = factory()

    first = store.put(
        "workspace/ws-1/cache item.bin",
        b"first",
        {"workspace_id": "ws-1", "voicebot_id": "bot-1", "kind": "test"},
    )
    testcase.assertEqual(store.get("workspace/ws-1/cache item.bin"), b"first")
    testcase.assertEqual(first.metadata["workspace_id"], "ws-1")

    second = store.put("workspace/ws-1/cache item.bin", b"second", {"workspace_id": "ws-1"})
    testcase.assertEqual(store.get("workspace/ws-1/cache item.bin"), b"second")
    testcase.assertEqual(first.path, second.path)
    testcase.assertGreaterEqual(len(store.list()), 1)

    testcase.assertTrue(store.delete("workspace/ws-1/cache item.bin"))
    testcase.assertIsNone(store.get("workspace/ws-1/cache item.bin"))
