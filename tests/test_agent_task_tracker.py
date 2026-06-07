from __future__ import annotations

import tempfile
import time
import unittest

from voicebot.agent_tasks import AgentTaskTracker, JsonAgentTaskTracker
from voicebot.storage.redis_agent_tasks import RedisAgentTaskTracker


class AgentTaskTrackerTests(unittest.TestCase):
    def test_claim_marks_events_not_pending_until_released(self) -> None:
        tracker = AgentTaskTracker()

        claimed = tracker.claim([1, 2], "worker-1", 30)

        self.assertEqual(claimed, [1, 2])
        self.assertFalse(tracker.is_pending(1))
        self.assertFalse(tracker.is_pending(2))
        self.assertEqual(tracker.release_many([1]), [1])
        self.assertTrue(tracker.is_pending(1))
        self.assertFalse(tracker.is_pending(2))

    def test_claim_skips_responded_events_and_clears_claim_on_response(self) -> None:
        tracker = AgentTaskTracker()
        tracker.claim([1], "worker-1", 30)

        tracker.mark_responded(1)

        self.assertFalse(tracker.is_pending(1))
        self.assertEqual(tracker.claim([1], "worker-2", 30), [])
        self.assertEqual(tracker.snapshot()["claims"], {})
        self.assertEqual(tracker.snapshot()["responded_event_ids"], [1])

    def test_claim_expires(self) -> None:
        tracker = AgentTaskTracker()

        self.assertEqual(tracker.claim([1], "worker-1", 0.1), [1])
        time.sleep(0.12)

        self.assertTrue(tracker.is_pending(1))
        self.assertEqual(tracker.snapshot()["claims"], {})

    def test_release_ignores_unclaimed_events(self) -> None:
        tracker = AgentTaskTracker()

        self.assertEqual(tracker.release_many([1]), [])
        tracker.release(None)
        tracker.release(1)

        self.assertTrue(tracker.is_pending(1))

    def test_release_many_can_require_matching_owner(self) -> None:
        tracker = AgentTaskTracker()
        tracker.claim([1, 2], "worker-1", 30)

        self.assertEqual(tracker.release_many([1], owner="worker-2"), [])
        self.assertFalse(tracker.is_pending(1))
        self.assertEqual(tracker.release_many([1], owner="worker-1"), [1])
        self.assertTrue(tracker.is_pending(1))
        self.assertEqual(tracker.release_many([2]), [2])

    def test_renew_many_extends_matching_owner_claims(self) -> None:
        tracker = AgentTaskTracker()
        tracker.claim([1], "worker-1", 0.1)

        self.assertEqual(tracker.renew_many([1], "worker-2", 30), [])
        self.assertEqual(tracker.renew_many([1], "worker-1", 30), [1])
        time.sleep(0.12)

        self.assertFalse(tracker.is_pending(1))

    def test_snapshot_can_filter_claims_by_owner(self) -> None:
        tracker = AgentTaskTracker()
        tracker.claim([1], "worker-1", 30)
        tracker.claim([2], "worker-2", 30)

        snapshot = tracker.snapshot(owner="worker-1")

        self.assertEqual(list(snapshot["claims"]), ["1"])
        self.assertEqual(snapshot["claims"]["1"]["owner"], "worker-1")

    def test_task_state_reports_pending_claimed_responded_and_inactive(self) -> None:
        tracker = AgentTaskTracker()

        self.assertEqual(tracker.task_state(1), {"state": "pending"})
        self.assertEqual(tracker.task_state(2, active=False), {"state": "inactive"})
        tracker.claim([3], "worker-1", 30)
        self.assertEqual(tracker.task_state(3)["state"], "claimed")
        self.assertEqual(tracker.task_state(3)["owner"], "worker-1")
        tracker.mark_responded(4)
        self.assertEqual(tracker.task_state(4), {"state": "responded"})

    def test_responded_events_are_bounded(self) -> None:
        tracker = AgentTaskTracker(max_responded_event_ids=2)

        tracker.mark_responded(1)
        tracker.mark_responded(2)
        tracker.mark_responded(3)

        self.assertFalse(tracker.is_pending(1))
        self.assertFalse(tracker.is_pending(2))
        self.assertFalse(tracker.is_pending(3))
        self.assertEqual(tracker.claim([1], "worker-1", 30), [])
        self.assertEqual(tracker.snapshot()["responded_event_ids"], [2, 3])
        self.assertEqual(tracker.snapshot()["responded_event_id_retention"], 2)
        self.assertEqual(tracker.snapshot()["responded_event_id_floor"], 1)

    def test_responded_event_retention_requires_positive_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_responded_event_ids must be at least 1"):
            AgentTaskTracker(max_responded_event_ids=0)

    def test_json_tracker_persists_responded_events_and_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/agent_tasks.json"
            first = JsonAgentTaskTracker(path)
            first.claim([1], "worker-1", 30)
            first.mark_responded(2)

            reloaded = JsonAgentTaskTracker(path)

        self.assertEqual(reloaded.snapshot()["responded_event_ids"], [2])
        self.assertFalse(reloaded.is_pending(1))
        self.assertEqual(reloaded.task_state(1)["state"], "claimed")
        self.assertEqual(reloaded.task_state(1)["owner"], "worker-1")

    def test_json_tracker_drops_expired_claims_on_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/agent_tasks.json"
            first = JsonAgentTaskTracker(path)
            first.claim([1], "worker-1", 0.1)
            time.sleep(0.12)

            reloaded = JsonAgentTaskTracker(path)

        self.assertTrue(reloaded.is_pending(1))
        self.assertEqual(reloaded.snapshot()["claims"], {})

    def test_redis_tracker_claims_and_responded_ids_use_shared_client(self) -> None:
        client = FakeRedis()
        first = RedisAgentTaskTracker("redis://test", client=client, max_responded_event_ids=2)
        second = RedisAgentTaskTracker("redis://test", client=client, max_responded_event_ids=2)

        self.assertEqual(first.claim([1, 2], "worker-1", 30), [1, 2])
        self.assertEqual(second.claim([1], "worker-2", 30), [])
        self.assertEqual(second.release_many([1], owner="worker-2"), [])
        self.assertEqual(second.release_many([1], owner="worker-1"), [1])
        self.assertEqual(second.claim([1], "worker-2", 30), [1])

        first.mark_responded(1)

        self.assertEqual(second.claim([1], "worker-3", 30), [])
        self.assertEqual(second.task_state(1), {"state": "responded"})
        self.assertEqual(second.snapshot()["responded_event_ids"], [1])

    def test_redis_tracker_claims_expire(self) -> None:
        tracker = RedisAgentTaskTracker("redis://test", client=FakeRedis())

        self.assertEqual(tracker.claim([1], "worker-1", 1), [1])
        self.assertFalse(tracker.is_pending(1))
        time.sleep(1.1)

        self.assertTrue(tracker.is_pending(1))

    def test_redis_tracker_prunes_responded_ids(self) -> None:
        tracker = RedisAgentTaskTracker("redis://test", client=FakeRedis(), max_responded_event_ids=2)

        tracker.mark_responded(1)
        tracker.mark_responded(2)
        tracker.mark_responded(3)

        self.assertEqual(tracker.snapshot()["responded_event_ids"], [2, 3])
        self.assertEqual(tracker.snapshot()["responded_event_id_floor"], 1)
        self.assertFalse(tracker.is_pending(1))


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
