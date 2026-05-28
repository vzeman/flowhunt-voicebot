from __future__ import annotations

import unittest

from voicebot.events import EventStore


class EventCompactionTests(unittest.TestCase):
    def test_compaction_preserves_flowhunt_completion_in_summary(self) -> None:
        events = EventStore(max_context_events=2)
        events.append(
            "call-1",
            "flowhunt_issue_completed",
            {
                "ok": True,
                "message": "Sitemap analysis complete. Total pages found: 1,950.",
                "issue_id": "issue-1",
            },
        )
        events.append("call-1", "user_transcript", {"text": "Is it ready?"})
        events.append("call-1", "agent_response_requested", {"text": "Is it ready?"})

        context = events.context(call_id="call-1")

        self.assertIn("flowhunt_issue_completed", context["summary"])
        self.assertIn("1,950", context["summary"])


if __name__ == "__main__":
    unittest.main()
