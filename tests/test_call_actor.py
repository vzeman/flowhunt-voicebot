from __future__ import annotations

import threading
import unittest

from voicebot.call_actor import CallActorCoordinator


class CallActorCoordinatorTests(unittest.TestCase):
    def test_lane_progress_is_isolated(self) -> None:
        coordinator = CallActorCoordinator("call-1")

        coordinator.started("stt", correlation_id="turn-1")
        coordinator.started("background", correlation_id="task-1")
        coordinator.cancel("tts_playback", reason="barge_in", correlation_id="turn-2")

        lanes = coordinator.snapshot()["lanes"]
        self.assertEqual(lanes["stt"]["active"], 1)
        self.assertEqual(lanes["background"]["active"], 1)
        self.assertEqual(lanes["tts_playback"]["active"], 0)
        self.assertEqual(lanes["tts_playback"]["cancellation_generation"], 1)

    def test_cancel_clears_only_selected_lane(self) -> None:
        coordinator = CallActorCoordinator("call-1")

        coordinator.queued("agent", correlation_id="event-1")
        coordinator.started("agent", correlation_id="event-1")
        coordinator.queued("background", correlation_id="task-1")
        signal = coordinator.cancel("agent", reason="stale_turn", correlation_id="event-2")

        lanes = coordinator.snapshot()["lanes"]
        self.assertEqual(signal.type, "cancelled")
        self.assertEqual(lanes["agent"]["queued"], 0)
        self.assertEqual(lanes["agent"]["active"], 0)
        self.assertEqual(lanes["background"]["queued"], 1)

    def test_concurrent_lanes_can_progress_without_shared_blocking(self) -> None:
        coordinator = CallActorCoordinator("call-1")

        def run_lane(lane: str) -> None:
            for index in range(20):
                coordinator.queued(lane, correlation_id=str(index))  # type: ignore[arg-type]
                coordinator.started(lane, correlation_id=str(index))  # type: ignore[arg-type]
                coordinator.completed(lane, correlation_id=str(index))  # type: ignore[arg-type]

        threads = [
            threading.Thread(target=run_lane, args=("stt",)),
            threading.Thread(target=run_lane, args=("tts_playback",)),
            threading.Thread(target=run_lane, args=("background",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        lanes = coordinator.snapshot()["lanes"]
        self.assertEqual(lanes["stt"]["active"], 0)
        self.assertEqual(lanes["tts_playback"]["active"], 0)
        self.assertEqual(lanes["background"]["active"], 0)
        self.assertEqual(lanes["stt"]["last_signal"]["type"], "completed")

    def test_unknown_lane_is_rejected(self) -> None:
        coordinator = CallActorCoordinator("call-1")

        with self.assertRaisesRegex(ValueError, "unknown actor lane"):
            coordinator.started("missing")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
