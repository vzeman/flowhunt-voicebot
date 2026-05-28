from __future__ import annotations

import tempfile
import time
import unittest

from voicebot.agent_tasks import AgentTaskTracker, JsonAgentTaskTracker


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


if __name__ == "__main__":
    unittest.main()
