from __future__ import annotations

import time
from typing import Any, Callable


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
