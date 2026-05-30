from __future__ import annotations

import unittest

from voicebot.progress import ProgressCadenceMemory, normalize_progress_message


class ProgressCadenceTests(unittest.TestCase):
    def test_normalizes_provider_status_tokens(self) -> None:
        self.assertEqual(
            normalize_progress_message("Current status is TaskStatus.WAITING_FOR_WORKER."),
            "Current status is still working.",
        )
        self.assertEqual(
            normalize_progress_message("FlowStatus.SUCCESS"),
            "completed",
        )

    def test_empty_or_raw_pending_progress_becomes_customer_facing(self) -> None:
        self.assertEqual(normalize_progress_message(""), "The colleague is still working on it.")
        self.assertEqual(normalize_progress_message("TaskStatus.PENDING"), "The colleague is still working on it.")

    def test_suppresses_duplicate_message_for_same_progress_key(self) -> None:
        memory = ProgressCadenceMemory(default_interval_seconds=5.0)

        self.assertTrue(memory.should_speak("call-1:task-1", "Still working.", now=10.0))
        self.assertFalse(memory.should_speak("call-1:task-1", "Still working.", now=20.0))

    def test_suppresses_distinct_message_until_interval_expires(self) -> None:
        memory = ProgressCadenceMemory(default_interval_seconds=5.0)

        self.assertTrue(memory.should_speak("call-1:task-1", "Checking.", now=10.0))
        self.assertFalse(memory.should_speak("call-1:task-1", "Still checking.", now=12.0))
        self.assertTrue(memory.should_speak("call-1:task-1", "Still checking.", now=16.0))

    def test_progress_keys_are_independent(self) -> None:
        memory = ProgressCadenceMemory(default_interval_seconds=5.0)

        self.assertTrue(memory.should_speak("call-1:task-1", "Checking.", now=10.0))
        self.assertTrue(memory.should_speak("call-1:task-2", "Checking.", now=10.0))


if __name__ == "__main__":
    unittest.main()
