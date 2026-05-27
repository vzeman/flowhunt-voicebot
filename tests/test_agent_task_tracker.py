from __future__ import annotations

import time
import unittest

from voicebot.agent_tasks import AgentTaskTracker


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


if __name__ == "__main__":
    unittest.main()
