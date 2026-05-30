from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import call, patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from local_command_agent import (
    action_acknowledgement,
    attach_response_event_id,
    attach_task_context,
    build_prompt,
    call_control_validation_prompt,
    colleague_update_answer,
    customer_facing_colleague_text,
    ensure_action_acknowledgements,
    execute_conversational_tool_calls,
    fast_tool_call,
    fast_tool_calls,
    filter_grounded_call_control_tools,
    needs_spoken_followup,
    parse_call_control_validation,
    remove_colleague_reentrant_tool_calls,
)


class AgentCoordinationTests(unittest.TestCase):
    def test_attach_response_event_id_overwrites_stale_model_value(self) -> None:
        calls = [{"name": "hangup_call", "arguments": {"call_id": "call-1", "response_to_event_id": 3042}}]

        sanitized = attach_response_event_id(calls, 3073)

        self.assertEqual(sanitized[0]["arguments"]["response_to_event_id"], 3073)

    def test_attach_task_context_overwrites_stale_model_call_context(self) -> None:
        task = {"id": 3073, "call_id": "webrtc-current", "data": {"text": "Check LiveAgent status page."}}
        calls = [
            {
                "name": "hangup_call",
                "arguments": {"call_id": "webrtc-old", "response_to_event_id": 3042},
            }
        ]

        sanitized = attach_task_context(calls, task)

        self.assertEqual(sanitized[0]["arguments"]["call_id"], "webrtc-current")
        self.assertEqual(sanitized[0]["arguments"]["response_to_event_id"], 3073)

    def test_call_control_validation_parser_accepts_json_true_only(self) -> None:
        self.assertTrue(parse_call_control_validation('{"allowed": true, "reason": "explicit"}'))
        self.assertFalse(parse_call_control_validation('{"allowed": false, "reason": "noise"}'))
        self.assertFalse(parse_call_control_validation("not json"))

    def test_call_control_validation_prompt_uses_current_message(self) -> None:
        prompt = call_control_validation_prompt(
            {"id": 3165, "call_id": "call-1", "data": {"text": "The dog"}},
            {"name": "hangup_call", "arguments": {"call_id": "call-1"}},
        )

        self.assertIn("The dog", prompt)
        self.assertIn("Reject if the message is noise", prompt)

    def test_ungrounded_call_control_tool_is_dropped(self) -> None:
        task = {"id": 3165, "call_id": "call-1", "data": {"text": "The dog"}}
        tool_calls = [
            {"name": "hangup_call", "arguments": {"call_id": "call-1", "response_to_event_id": 3165}},
            {"name": "say", "arguments": {"call_id": "call-1", "text": "I am still here."}},
        ]

        filtered = filter_grounded_call_control_tools(tool_calls, task, lambda _task, _call: False)

        self.assertEqual([call["name"] for call in filtered], ["say"])

    def test_explicit_call_control_tool_is_kept_after_validation(self) -> None:
        task = {"id": 3165, "call_id": "call-1", "data": {"text": "Please hang up now."}}
        tool_calls = [{"name": "hangup_call", "arguments": {"call_id": "call-1", "response_to_event_id": 3165}}]

        filtered = filter_grounded_call_control_tools(tool_calls, task, lambda _task, _call: True)

        self.assertEqual(filtered, tool_calls)

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

    def test_build_prompt_includes_voicebot_prompt_config(self) -> None:
        prompt = build_prompt(
            [{"id": 10, "call_id": "call-1", "data": {"text": "Ahoj"}}],
            {
                "voicebot_prompts": {
                    "greeting": "Pozdrav volajuceho po slovensky.",
                    "system_prompt": "Use concise Slovak.",
                    "stt_prompt": "LiveAgent",
                    "language": "sk",
                }
            },
            [],
        )

        self.assertIn("Default response language: sk", prompt)
        self.assertIn("Use concise Slovak.", prompt)

    def test_build_prompt_auto_language_instructs_mirroring_caller_language(self) -> None:
        prompt = build_prompt(
            [{"id": 10, "call_id": "call-1", "data": {"text": "Dobrý deň"}}],
            {"voicebot_prompts": {"language": "auto"}},
            [],
        )

        self.assertIn("Detect the caller's language", prompt)
        self.assertIn("switch with them", prompt)

    def test_call_connected_with_custom_prompt_uses_model_turn(self) -> None:
        task = {
            "id": 10,
            "call_id": "call-1",
            "data": {"reason": "call_connected", "text": "Pozdrav volajuceho po slovensky.", "use_agent_prompt": True},
        }

        self.assertEqual(fast_tool_calls(task), [])

    def test_flowhunt_invocation_does_not_need_immediate_followup(self) -> None:
        self.assertFalse(needs_spoken_followup([{"name": "invoke_flowhunt_flow", "arguments": {}}]))

    def test_colleague_result_fast_path_marks_response_as_persistent_result(self) -> None:
        calls = fast_tool_calls(
            {
                "id": 10,
                "call_id": "call-1",
                "data": {
                    "reason": "colleague_result",
                    "data": {"summary": "The latest LiveAgent incident was resolved in 56 minutes."},
                },
            }
        )

        self.assertEqual(calls[0]["arguments"]["response_kind"], "colleague_result")

    def test_caller_text_is_not_interpreted_by_fast_path(self) -> None:
        task = {
            "id": 10,
            "call_id": "call-1",
            "data": {"text": "Please hang up the call."},
        }

        calls = fast_tool_calls(task)

        self.assertEqual(calls, [])

    def test_action_acknowledgement_is_added_for_transfer(self) -> None:
        action = {
            "name": "transfer_call",
            "arguments": {"call_id": "call-1", "target": "123", "response_to_event_id": 10},
        }

        calls = ensure_action_acknowledgements([action])

        self.assertEqual([call["name"] for call in calls], ["say", "transfer_call"])
        self.assertEqual(calls[0]["arguments"]["text"], "Transferring to 123 now.")
        self.assertEqual(calls[0]["arguments"]["response_kind"], "call_control_ack")
        self.assertEqual(calls[0]["arguments"]["call_control_ack_wait_seconds"], 0.0)

    def test_action_acknowledgement_is_short_for_hangup(self) -> None:
        action = {
            "name": "hangup_call",
            "arguments": {"call_id": "call-1", "response_to_event_id": 10},
        }

        calls = ensure_action_acknowledgements([action])

        self.assertEqual(calls[0]["arguments"]["text"], "Goodbye.")
        self.assertEqual(calls[0]["arguments"]["response_kind"], "call_control_ack")
        self.assertEqual(calls[0]["arguments"]["call_control_ack_wait_seconds"], 1.2)

    def test_action_acknowledgement_uses_existing_say(self) -> None:
        calls = ensure_action_acknowledgements(
            [
                {"name": "say", "arguments": {"call_id": "call-1", "text": "I will do that."}},
                {"name": "hangup_call", "arguments": {"call_id": "call-1"}},
            ]
        )

        self.assertEqual([call["name"] for call in calls], ["say", "hangup_call"])
        self.assertEqual(calls[0]["arguments"]["response_kind"], "call_control_ack")
        self.assertEqual(calls[0]["arguments"]["call_control_ack_wait_seconds"], 1.2)

    def test_execute_conversational_tool_calls_waits_after_say_before_control(self) -> None:
        calls = [
            {
                "name": "say",
                "arguments": {
                    "call_id": "call-1",
                    "text": "Goodbye.",
                    "response_kind": "call_control_ack",
                    "call_control_ack_wait_seconds": 1.2,
                },
            },
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

    def test_execute_conversational_tool_calls_can_run_control_while_speech_is_playing(self) -> None:
        calls = [
            {
                "name": "say",
                "arguments": {
                    "call_id": "call-1",
                    "text": "Transferring now.",
                    "response_kind": "call_control_ack",
                    "call_control_ack_wait_seconds": 0.0,
                },
            },
            {"name": "transfer_call", "arguments": {"call_id": "call-1", "target": "123"}},
        ]

        with patch("local_command_agent.http_json") as http_json:
            http_json.return_value = {"ok": True}

            results = execute_conversational_tool_calls("http://voicebot", calls)

        self.assertEqual([result["name"] for result in results], ["say", "transfer_call"])
        http_json.assert_has_calls(
            [
                call("POST", "http://voicebot/agent/tools/say", {"arguments": calls[0]["arguments"]}),
                call("POST", "http://voicebot/agent/tools/transfer_call", {"arguments": calls[1]["arguments"]}),
            ]
        )
        self.assertEqual(http_json.call_count, 2)

    def test_execute_conversational_tool_calls_runs_background_work_during_speech(self) -> None:
        calls = [
            {"name": "say", "arguments": {"call_id": "call-1", "text": "I am checking that."}},
            {
                "name": "invoke_flowhunt_flow",
                "arguments": {"call_id": "call-1", "message": "Check status."},
            },
        ]
        timings = {}

        def fake_http_json(method, url, payload=None):
            if url.endswith("/agent/tools/say"):
                timings["say_start"] = time.monotonic()
                time.sleep(0.25)
                timings["say_end"] = time.monotonic()
                return {"ok": True}
            if url.endswith("/agent/tools/invoke_flowhunt_flow"):
                timings["work_start"] = time.monotonic()
                return {"ok": True}
            raise AssertionError(url)

        with patch("local_command_agent.http_json", side_effect=fake_http_json):
            results = execute_conversational_tool_calls("http://voicebot", calls)

        self.assertEqual([result["name"] for result in results], ["say", "invoke_flowhunt_flow"])
        self.assertLess(timings["work_start"], timings["say_end"])

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
            "Connect the VoIP number first, then assign an IVR tree.",
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
            "Small Business does not include call center functionality.",
        )

    def test_colleague_result_keeps_spoken_answer_short_and_drops_followup_prompts(self) -> None:
        task = {
            "id": 10,
            "call_id": "call-1",
            "data": {
                "reason": "colleague_result",
                "data": {
                    "summary": (
                        "LiveAgent currently has normal status and no active incident is visible. "
                        "The latest visible incident was resolved earlier. "
                        "If you tell me which region you use, I can check more details. "
                        "Internal note: raw monitoring response contained 25 checks."
                    )
                },
            },
        }

        answer = colleague_update_answer(task)

        self.assertLessEqual(len(answer), 190)
        self.assertIn("normal operation", answer)
        self.assertNotIn("If you tell me", answer)
        self.assertNotIn("Internal note", answer)

    def test_colleague_result_extracts_complete_incident_summary(self) -> None:
        task = {
            "id": 10,
            "call_id": "call-1",
            "data": {
                "reason": "colleague_result",
                "data": {
                    "summary": (
                        "Based on the LiveAgent status page archive, I can confirm the following:\n\n"
                        "May 6th, 2026 - Downtime Confirmed:\n"
                        "Yes, there was a service incident on May 6th, 2026. It was an agent panel "
                        "slowness in the Europe (Frankfurt) data center, not a complete outage.\n\n"
                        "Details:\n"
                        "Issue: Agent panel slowness affecting EU Frankfurt data center\n"
                        "Duration: Resolved in 56 minutes\n"
                        "Cause: A sick node in the infrastructure\n"
                        "Status: Fully resolved\n"
                    )
                },
            },
        }

        answer = colleague_update_answer(task)

        self.assertEqual(
            answer,
            "Yes, May 6th, 2026 was a service degradation in EU Frankfurt: Agent panel slowness, "
            "resolved in 56 minutes.",
        )

    def test_colleague_result_does_not_turn_normal_status_page_into_incident(self) -> None:
        task = {
            "id": 10,
            "call_id": "call-1",
            "data": {
                "reason": "colleague_result",
                "data": {
                    "summary": (
                        "Based on the current status page, there have been no incidents in the last 90 days. "
                        "All LiveAgent services are operating normally. The status page shows no downtime "
                        "recorded from May 18 through May 30, 2026."
                    )
                },
            },
        }

        answer = colleague_update_answer(task)

        self.assertEqual(
            answer,
            "The LiveAgent status page currently shows normal operation, with no active downtime or visible incidents.",
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

        self.assertEqual(colleague_update_answer(task), "I am still checking it.")

    def test_colleague_update_with_consume_prompt_uses_model_path(self) -> None:
        task = {
            "id": 10,
            "call_id": "call-1",
            "data": {
                "reason": "colleague_result",
                "consume_prompt": "Turn this colleague result into a concise customer answer.",
                "data": {"summary": "Raw colleague result."},
            },
        }

        self.assertEqual(fast_tool_calls(task), [])

    def test_colleague_update_with_non_english_session_language_uses_model_path(self) -> None:
        task = {
            "id": 10,
            "call_id": "call-1",
            "data": {
                "reason": "colleague_result",
                "session_language": {"language": "sk", "confidence": 0.95},
                "data": {"summary": "Všetky služby fungujú normálne."},
            },
        }

        self.assertEqual(fast_tool_calls(task), [])


if __name__ == "__main__":
    unittest.main()
