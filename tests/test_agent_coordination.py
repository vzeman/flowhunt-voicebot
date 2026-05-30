from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from local_command_agent import (
    action_acknowledgement,
    colleague_update_answer,
    customer_facing_colleague_text,
    ensure_action_acknowledgements,
    execute_conversational_tool_calls,
    fast_tool_call,
    fast_tool_calls,
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

    def test_hangup_fast_path_speaks_before_call_control(self) -> None:
        task = {
            "id": 10,
            "call_id": "call-1",
            "data": {"text": "Please hang up the call."},
        }

        calls = fast_tool_calls(task)

        self.assertEqual([call["name"] for call in calls], ["say", "hangup_call"])
        self.assertEqual(calls[0]["arguments"]["text"], "Sure, I will hang up the call now. Goodbye.")
        self.assertEqual(calls[0]["arguments"]["response_to_event_id"], 10)
        self.assertEqual(calls[1]["arguments"]["response_to_event_id"], 10)

    def test_action_acknowledgement_is_added_for_transfer(self) -> None:
        action = {
            "name": "transfer_call",
            "arguments": {"call_id": "call-1", "target": "123", "response_to_event_id": 10},
        }

        calls = ensure_action_acknowledgements([action])

        self.assertEqual([call["name"] for call in calls], ["say", "transfer_call"])
        self.assertIn("transfer you to 123", calls[0]["arguments"]["text"])

    def test_action_acknowledgement_uses_existing_say(self) -> None:
        calls = ensure_action_acknowledgements(
            [
                {"name": "say", "arguments": {"call_id": "call-1", "text": "I will do that."}},
                {"name": "hangup_call", "arguments": {"call_id": "call-1"}},
            ]
        )

        self.assertEqual([call["name"] for call in calls], ["say", "hangup_call"])

    def test_execute_conversational_tool_calls_waits_after_say_before_control(self) -> None:
        calls = [
            {"name": "say", "arguments": {"call_id": "call-1", "text": "Goodbye."}},
            {"name": "hangup_call", "arguments": {"call_id": "call-1"}},
        ]

        with patch("local_command_agent.http_json") as http_json:
            http_json.side_effect = [
                {"ok": True},
                {"call_id": "call-1", "playback_active": True},
                {"call_id": "call-1", "playback_active": False},
                {"ok": True},
            ]

            results = execute_conversational_tool_calls("http://voicebot", calls)

        self.assertEqual([result["name"] for result in results], ["say", "hangup_call"])
        http_json.assert_has_calls(
            [
                call("POST", "http://voicebot/agent/tools/say", {"arguments": calls[0]["arguments"]}),
                call("GET", "http://voicebot/calls/call-1"),
                call("GET", "http://voicebot/calls/call-1"),
                call("POST", "http://voicebot/agent/tools/hangup_call", {"arguments": calls[1]["arguments"]}),
            ]
        )

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

    def test_colleague_result_strips_emoji_greeting_and_summarizes_status_page(self) -> None:
        raw = """
        👋 Hello and welcome to LiveAgent Support!
        I'm AI chatbot assistant ready to assist with any questions about LiveAgent using our knowledge base.
        Got any questions? Feel free to ask in your preferred language!

        I checked **https://status.liveagent.com/** and it currently indicates **normal status**.
        - Live status: Normal (operational)
        - Downtime: No active downtime is shown
        - Notable incidents: None visible
        Reference: https://status.liveagent.com/
        """

        answer = customer_facing_colleague_text(raw)

        self.assertEqual(
            answer,
            "The LiveAgent status page currently shows normal operation, with no active downtime or visible incidents.",
        )

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
