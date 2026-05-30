from __future__ import annotations

import time
import unittest

from voicebot.events import EventStore
from voicebot.turn_coalescing import TurnCoalescer, coalesce_text


class TurnCoalescingTests(unittest.TestCase):
    def test_coalesces_short_adjacent_turns_into_one_agent_request(self) -> None:
        events = EventStore(max_context_events=20)
        requests: list[dict] = []
        coalescer = TurnCoalescer(
            call_id=lambda: "call-1",
            events=events,
            emit_request=lambda data: requests.append(data),
            can_delay_or_merge=lambda: True,
            window_seconds=0.5,
            max_chars=80,
        )

        self.assertIsNone(coalescer.handle({"turn_id": 1, "transcript_event_id": 10, "text": "How many pages"}))
        coalescer.handle({"turn_id": 2, "transcript_event_id": 11, "text": "on liveagent.com"})

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["text"], "How many pages on liveagent.com")
        self.assertTrue(requests[0]["coalesced"])
        self.assertEqual(requests[0]["coalesced_turn_ids"], [1, 2])
        self.assertEqual(events.list_events(call_id="call-1")[-1].type, "turn_coalesced")

    def test_flushes_after_window_when_no_followup_arrives(self) -> None:
        requests: list[dict] = []
        coalescer = TurnCoalescer(
            call_id=lambda: "call-1",
            events=EventStore(max_context_events=20),
            emit_request=lambda data: requests.append(data),
            can_delay_or_merge=lambda: True,
            window_seconds=0.02,
            max_chars=80,
        )

        coalescer.handle({"turn_id": 1, "transcript_event_id": 10, "text": "hello"})
        time.sleep(0.06)

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["text"], "hello")
        self.assertEqual(requests[0]["coalesce_flush_reason"], "coalesce_window_elapsed")

    def test_does_not_delay_when_playback_or_response_is_active(self) -> None:
        requests: list[dict] = []
        coalescer = TurnCoalescer(
            call_id=lambda: "call-1",
            events=EventStore(max_context_events=20),
            emit_request=lambda data: requests.append(data),
            can_delay_or_merge=lambda: False,
            window_seconds=0.5,
            max_chars=80,
        )

        coalescer.handle({"turn_id": 1, "transcript_event_id": 10, "text": "hello"})

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["text"], "hello")
        self.assertNotIn("coalesced", requests[0])

    def test_long_turns_are_not_delayed(self) -> None:
        requests: list[dict] = []
        coalescer = TurnCoalescer(
            call_id=lambda: "call-1",
            events=EventStore(max_context_events=20),
            emit_request=lambda data: requests.append(data),
            can_delay_or_merge=lambda: True,
            window_seconds=0.5,
            max_chars=5,
        )

        coalescer.handle({"turn_id": 1, "text": "this is already complete"})

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["text"], "this is already complete")

    def test_separator_keeps_comma_fragments_together(self) -> None:
        self.assertEqual(coalesce_text("Please check,", "the sitemap"), "Please check, the sitemap")


if __name__ == "__main__":
    unittest.main()
