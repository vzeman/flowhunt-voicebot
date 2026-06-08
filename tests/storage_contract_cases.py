from __future__ import annotations

import time
from typing import Any, Callable

from voicebot.scaling import RoutingKey, WorkerInstance, WorkerQueueEnvelope
from voicebot.subagents import SubagentTaskRequest, SubagentTaskResult


def assert_event_store_contract(testcase: Any, factory: Callable[[], Any]) -> None:
    store = factory()
    try:
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
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


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


def assert_worker_queue_store_contract(testcase: Any, factory: Callable[[], Any]) -> None:
    store = factory()
    envelope = WorkerQueueEnvelope(
        item_id="item-1",
        kind="agent_turn",
        routing=RoutingKey(workspace_id="ws-1", voicebot_id="bot-1", session_id="call-1"),
        queue="voicebot.agent",
        payload={"event_id": 7},
        idempotency_key="call-1:event-7",
        max_attempts=2,
    )
    duplicate = WorkerQueueEnvelope(
        item_id="item-2",
        kind="agent_turn",
        routing=RoutingKey(workspace_id="ws-1", voicebot_id="bot-1", session_id="call-1"),
        queue="voicebot.agent",
        payload={"event_id": 7},
        idempotency_key="call-1:event-7",
        max_attempts=2,
    )

    testcase.assertEqual(store.enqueue(envelope).item_id, "item-1")
    testcase.assertEqual(store.enqueue(duplicate).item_id, "item-1")
    testcase.assertEqual([item.item_id for item in store.pending("voicebot.agent")], ["item-1"])

    claimed = store.claim("voicebot.agent", "worker-1", ttl_seconds=30)
    testcase.assertEqual([item.item_id for item in claimed], ["item-1"])
    testcase.assertEqual(claimed[0].attempt, 1)
    testcase.assertIsNotNone(store.renew("item-1", "worker-1", ttl_seconds=30))
    testcase.assertIsNone(store.ack("item-1", owner="worker-2"))

    released = store.release("item-1", owner="worker-1", error="retry")
    testcase.assertEqual(released.last_error, "retry")
    testcase.assertEqual(store.claim("voicebot.agent", "worker-1")[0].attempt, 2)
    testcase.assertIsNotNone(store.ack("item-1", owner="worker-1"))
    testcase.assertEqual(store.pending(), ())


def assert_subagent_task_store_contract(testcase: Any, factory: Callable[[], Any]) -> None:
    store = factory()
    request = SubagentTaskRequest(
        workspace_id="ws-1",
        voicebot_id="bot-1",
        session_id="call-1",
        request_event_id=7,
        provider="internal_worker",
        input_text="check this",
    )

    task, created = store.get_or_create_requested(request)
    duplicate, duplicate_created = store.get_or_create_requested(request)

    testcase.assertTrue(created)
    testcase.assertFalse(duplicate_created)
    testcase.assertEqual(duplicate.task_id, task.task_id)
    testcase.assertIsNone(store.get(task.task_id, workspace_id="ws-2"))
    testcase.assertEqual([item.task_id for item in store.list(workspace_id="ws-1", session_id="call-1")], [task.task_id])

    running = store.update(task.with_status("running", external_task_id="external-1", progress_message="started"))
    testcase.assertEqual(running.external_task_id, "external-1")
    testcase.assertEqual(store.get(task.task_id).progress_messages, ("started",))
    testcase.assertEqual([item.task_id for item in store.pending()], [task.task_id])

    completed = store.update(running.with_status("completed", result=SubagentTaskResult(summary="done")))
    testcase.assertEqual(completed.status, "completed")
    testcase.assertEqual(store.pending(), [])


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
