from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from local_command_agent import (
    colleague_update_answer,
    fast_tool_call,
    needs_spoken_followup,
    remove_colleague_reentrant_tool_calls,
)


class AgentCoordinationTests(unittest.TestCase):
    def test_colleague_result_cannot_invoke_another_colleague_task(self) -> None:
        tasks = [
            {
                "id": 10,
                "call_id": "call-1",
                "data": {
                    "reason": "colleague_result",
                    "text": "A FlowHunt colleague finished checking the caller request.",
                },
            }
        ]
        tool_calls = [
            {"name": "invoke_flowhunt_flow", "arguments": {"call_id": "call-1", "message": "check again"}},
            {"name": "say", "arguments": {"call_id": "call-1", "text": "Here is the result."}},
        ]

        filtered = remove_colleague_reentrant_tool_calls(tasks, tool_calls)

        self.assertEqual([call["name"] for call in filtered], ["say"])

    def test_flowhunt_invocation_does_not_need_immediate_followup(self) -> None:
        self.assertFalse(needs_spoken_followup([{"name": "invoke_flowhunt_flow", "arguments": {}}]))

    def test_colleague_result_text_is_not_treated_as_call_control(self) -> None:
        task = {
            "id": 10,
            "call_id": "call-1",
            "data": {
                "reason": "colleague_result",
                "text": "Result: LiveAgent IVR can transfer to departments based on menu choices.",
            },
        }

        call = fast_tool_call(task)

        self.assertEqual(call["name"], "say")
        self.assertEqual(call["arguments"]["response_to_event_id"], 10)
        self.assertIn("LiveAgent IVR can transfer", call["arguments"]["text"])

    def test_colleague_result_uses_clean_summary_for_fast_spoken_answer(self) -> None:
        task = {
            "id": 10,
            "call_id": "call-1",
            "data": {
                "reason": "colleague_result",
                "text": "A colleague finished checking the caller request. Result: raw fallback",
                "data": {"summary": "Connect the VoIP number first, then assign an IVR tree."},
            },
        }

        answer = colleague_update_answer(task)

        self.assertEqual(
            answer,
            "I checked with a colleague. Connect the VoIP number first, then assign an IVR tree.",
        )


if __name__ == "__main__":
    unittest.main()
