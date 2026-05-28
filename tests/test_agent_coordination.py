from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from local_command_agent import (
    colleague_update_answer,
    customer_facing_colleague_text,
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

    def test_colleague_result_strips_internal_status_before_speaking(self) -> None:
        raw = """
        status: completed
        task_id: abc-123
        Internal note: sitemap crawler used 4 requests.
        Final answer: LiveAgent has 1,950 sitemap pages. The sitemap index was counted directly.
        """

        answer = customer_facing_colleague_text(raw)

        self.assertEqual(answer, "LiveAgent has 1,950 sitemap pages. The sitemap index was counted directly.")

    def test_colleague_result_turns_pricing_table_into_customer_summary(self) -> None:
        raw = """
        Hello and welcome to LiveAgent Support!
        I'm AI chatbot assistant ready to assist with any questions.

        Here's the detailed pricing info for LiveAgent accounts with Call Center functionality you can share with your colleague:

        ## Call Center availability by plan
        - Small Business: $19/agent/month (monthly) or $15/agent/month (annual) - Call Center: not included
        - Medium Business: $35/agent/month (monthly) or $29/agent/month (annual) - Call Center: included
        - Large Business: $59/agent/month (monthly) or $49/agent/month (annual) - Call Center: included
        - Enterprise: $85/agent/month (monthly) or $69/agent/month (annual) - Call Center: included
        """

        answer = customer_facing_colleague_text(raw)

        self.assertEqual(
            answer,
            "Call center functionality is included starting with Medium Business, "
            "which is $35 per agent per month, or $29 annually. "
            "Large Business is $59 monthly, or $49 annually. "
            "Enterprise is $85 monthly, or $69 annually. "
            "Small Business does not include call center functionality.",
        )

    def test_colleague_progress_uses_canned_customer_update(self) -> None:
        task = {
            "id": 10,
            "call_id": "call-1",
            "data": {
                "reason": "colleague_progress",
                "text": "status: running task_id=abc internal polling still active",
            },
        }

        self.assertEqual(colleague_update_answer(task), "I am still checking that with a colleague.")


if __name__ == "__main__":
    unittest.main()
