from __future__ import annotations

from pathlib import Path
import sys
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from agent_provider_registry import AgentProviderRegistry
from communication_agent import (
    CommunicationAgentConfig,
    DelayedProgressAcknowledgement,
    finalize_streamed_response,
    colleague_progress_ack_text_for_task,
    colleague_progress_ack_tool_call,
    fallback_answer_for_dropped_tools,
    has_colleague_tool_call,
    parse_colleague_tool_recovery,
    preferred_colleague_tool_name,
    progress_ack_text_for_task,
    progress_ack_tool_call,
    recover_missing_colleague_tool_call,
    run_model_turn,
    run_model_turn_streaming,
    should_send_delayed_acknowledgement,
    should_prepend_colleague_progress_ack,
    split_stable_stream_text,
    submit_stream_chunk,
    suppress_colleague_tool_progress,
    has_http_failed_say,
    provider_failure_answer,
    run_provider_with_retry,
)
from voicebot.agent import AgentOutput


class CommunicationAgentProviderRecoveryTests(unittest.TestCase):
    def make_config(self) -> CommunicationAgentConfig:
        return CommunicationAgentConfig(
            base_url="http://voicebot",
            provider="test",
            model="model",
            interval=0.01,
            timeout=1.0,
            max_output_tokens=80,
            owner_prefix="test-agent",
        )

    def test_provider_call_is_retried_once(self) -> None:
        calls = 0

        def flaky_provider(client, model, prompt, timeout, max_output_tokens, tools):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise Exception("temporary provider failure")
            return "ok", []

        registry = AgentProviderRegistry()
        registry.register("test", flaky_provider)

        answer, tool_calls = run_provider_with_retry(object(), registry, self.make_config(), "prompt", [])

        self.assertEqual(answer, "ok")
        self.assertEqual(tool_calls, [])
        self.assertEqual(calls, 2)

    def test_provider_failure_answer_is_short_and_spoken(self) -> None:
        self.assertEqual(
            provider_failure_answer(Exception("server_error")),
            "I had a temporary AI error. Please repeat that once more.",
        )

    def test_provider_server_error_is_retried_once_for_realtime_turn(self) -> None:
        calls = 0

        def flaky_provider(client, model, prompt, timeout, max_output_tokens, tools):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise Exception("Error code: 500 - server_error")
            return "ok", []

        registry = AgentProviderRegistry()
        registry.register("test", flaky_provider)

        answer, tool_calls = run_provider_with_retry(object(), registry, self.make_config(), "prompt", [])

        self.assertEqual(answer, "ok")
        self.assertEqual(tool_calls, [])
        self.assertEqual(calls, 2)

    def test_model_turn_preserves_chat_payload_from_json_response(self) -> None:
        def provider(client, model, prompt, timeout, max_output_tokens, tools):
            return (
                '{"say":"Short spoken answer.","chat":{"text":"Longer readable answer.",'
                '"blocks":[{"type":"image","url":"https://example.com/image.png"}]}}',
                [],
            )

        registry = AgentProviderRegistry()
        registry.register("test", provider)

        answer, tool_calls, chat = run_model_turn(
            object(),
            registry,
            self.make_config(),
            "prompt",
            [{"id": 42, "call_id": "call-1", "data": {"text": "hello"}}],
            [],
        )

        self.assertEqual(answer, "Short spoken answer.")
        self.assertEqual(tool_calls, [])
        self.assertEqual(chat["text"], "Longer readable answer.")
        self.assertEqual(chat["blocks"][0]["type"], "image")

    def test_stable_stream_text_splits_on_sentence_or_size(self) -> None:
        ready, pending = split_stable_stream_text("Hello caller. I can help", 90)

        self.assertEqual(ready, ["Hello caller."])
        self.assertEqual(pending, "I can help")

        ready, pending = split_stable_stream_text("This is a longer chunk without punctuation", 12)

        self.assertEqual(ready, ["This is a", "longer", "chunk", "without"])
        self.assertEqual(pending, "punctuation")

    def test_streaming_model_turn_posts_spoken_chunks_and_finalizes_task(self) -> None:
        def provider(client, model, prompt, timeout, max_output_tokens, tools):
            raise AssertionError("non-streaming provider should not be used")

        def stream_provider(client, model, prompt, timeout, max_output_tokens, tools):
            yield AgentOutput("Hello ", is_final=False)
            yield AgentOutput("caller. ", is_final=False)
            yield AgentOutput("I can help.", is_final=False)
            yield AgentOutput(is_final=True)

        registry = AgentProviderRegistry()
        registry.register("test", provider)
        registry.register_stream("test", stream_provider)
        calls = []

        with unittest.mock.patch("communication_agent.http_json", side_effect=lambda method, url, payload=None: calls.append((method, url, payload)) or {"ok": True}):
            answer, tool_calls, streamed = run_model_turn_streaming(
                object(),
                registry,
                CommunicationAgentConfig(
                    base_url="http://voicebot",
                    provider="test",
                    model="model",
                    interval=0.01,
                    timeout=1.0,
                    max_output_tokens=80,
                    owner_prefix="test-agent",
                    streaming_enabled=True,
                    streaming_chunk_chars=90,
                ),
                "prompt",
                [{"id": 42, "call_id": "call-1", "data": {"text": "hello"}}],
                [],
                {"id": 42, "call_id": "call-1"},
            )

        self.assertEqual(answer, "")
        self.assertEqual(tool_calls, [])
        self.assertTrue(streamed)
        self.assertEqual(calls[0][1], "http://voicebot/calls/call-1/responses")
        self.assertTrue(calls[0][2]["partial"])
        self.assertEqual(calls[0][2]["text"], "Hello caller.")
        self.assertNotIn("finalize_only", calls[-1][2])

    def test_stream_chunk_and_finalize_use_response_protocol(self) -> None:
        calls = []

        with unittest.mock.patch("communication_agent.http_json", side_effect=lambda method, url, payload=None: calls.append((method, url, payload)) or {"ok": True}):
            submit_stream_chunk("http://voicebot", {"id": 7, "call_id": "call-1"}, "Hi")
            finalize_streamed_response("http://voicebot", {"id": 7, "call_id": "call-1"})

        self.assertTrue(calls[0][2]["partial"])
        self.assertEqual(calls[0][2]["response_kind"], "stream_chunk")
        self.assertTrue(calls[1][2]["finalize_only"])

    def test_failed_say_http_result_is_detected(self) -> None:
        self.assertTrue(has_http_failed_say([{"name": "say", "ok": False, "error": "HTTP Error 404"}]))
        self.assertFalse(has_http_failed_say([{"name": "say", "ok": True, "result": {"ok": False}}]))

    def test_delayed_ack_is_only_for_caller_requests(self) -> None:
        self.assertTrue(should_send_delayed_acknowledgement({"data": {"text": "Check status"}}))
        self.assertFalse(
            should_send_delayed_acknowledgement({"data": {"reason": "call_connected", "text": "connected"}})
        )
        self.assertFalse(
            should_send_delayed_acknowledgement({"data": {"reason": "colleague_result", "text": "done"}})
        )

    def test_delayed_ack_is_later_and_tagged_as_progress(self) -> None:
        ack = DelayedProgressAcknowledgement("http://voicebot", {"call_id": "call-1"}, delay_seconds=0.0)

        with unittest.mock.patch("communication_agent.http_json") as http_json:
            ack._run()

        http_json.assert_called_once_with(
            "POST",
            "http://voicebot/calls/call-1/responses",
            {
                "text": "Give me a moment.",
                "response_to_event_id": None,
                "response_kind": "progress_ack",
            },
        )

    def test_progress_ack_uses_default_prompt_when_no_configured_filler(self) -> None:
        task = {"id": 1, "call_id": "call-1", "data": {"session_language": {"language": "sk"}}}

        self.assertEqual(progress_ack_text_for_task(task), "Give me a moment.")
        self.assertEqual(progress_ack_tool_call(task)["arguments"]["text"], "Give me a moment.")

    def test_progress_ack_uses_configured_filler_message(self) -> None:
        task = {
            "id": 1,
            "call_id": "call-1",
            "data": {
                "prompt_config": {"filler_message": "I am checking that now."},
                "session_language": {"language": "sk"},
            },
        }

        self.assertEqual(progress_ack_text_for_task(task), "I am checking that now.")
        self.assertEqual(progress_ack_tool_call(task)["arguments"]["text"], "I am checking that now.")

    def test_colleague_progress_ack_explicitly_mentions_colleague(self) -> None:
        task = {"id": 1, "call_id": "call-1", "data": {}}

        self.assertIn("colleague", colleague_progress_ack_text_for_task(task))

    def test_colleague_progress_ack_uses_configured_prompt(self) -> None:
        task = {
            "id": 1,
            "call_id": "call-1",
            "data": {"prompt_config": {"colleague_progress_message": "I asked our billing specialist."}},
        }

        self.assertEqual(colleague_progress_ack_text_for_task(task), "I asked our billing specialist.")
        self.assertEqual(colleague_progress_ack_tool_call(task)["arguments"]["text"], "I asked our billing specialist.")
        self.assertEqual(colleague_progress_ack_tool_call(task)["arguments"]["response_kind"], "progress_ack")

    def test_dropped_tool_fallback_asks_for_full_request(self) -> None:
        task = {"id": 1, "call_id": "call-1", "data": {"text": "pricing."}}

        self.assertEqual(
            fallback_answer_for_dropped_tools(task),
            "I heard: pricing. Could you please say the full request again?",
        )

    def test_colleague_progress_ack_is_prepended_when_model_only_calls_tool(self) -> None:
        task = {"id": 1, "call_id": "call-1", "data": {"text": "Check it"}}

        self.assertTrue(
            should_prepend_colleague_progress_ack(
                task,
                [{"name": "invoke_flowhunt_flow", "arguments": {"call_id": "call-1", "message": "Check it"}}],
                initial_say=None,
                delayed_ack_delivered=False,
                streamed_response=False,
            )
        )
        self.assertFalse(
            should_prepend_colleague_progress_ack(
                task,
                [{"name": "invoke_flowhunt_flow", "arguments": {"call_id": "call-1", "message": "Check it"}}],
                initial_say={"name": "say", "arguments": {"text": "I will check."}},
                delayed_ack_delivered=False,
                streamed_response=False,
            )
        )
        self.assertFalse(
            should_prepend_colleague_progress_ack(
                task,
                [{"name": "invoke_flowhunt_flow", "arguments": {"call_id": "call-1", "message": "Check it"}}],
                initial_say=None,
                delayed_ack_delivered=True,
                streamed_response=False,
            )
        )

    def test_colleague_tool_progress_can_be_suppressed_after_delayed_ack(self) -> None:
        calls = [
            {
                "name": "invoke_flowhunt_flow",
                "arguments": {"call_id": "call-1", "message": "Check status."},
            },
            {"name": "say", "arguments": {"call_id": "call-1", "text": "Done."}},
        ]

        updated = suppress_colleague_tool_progress(calls)

        self.assertTrue(updated[0]["arguments"]["suppress_progress"])
        self.assertNotIn("suppress_progress", updated[1]["arguments"])

    def test_colleague_tool_call_is_detected(self) -> None:
        self.assertTrue(has_colleague_tool_call([{"name": "delegate_to_subagent", "arguments": {}}]))
        self.assertFalse(has_colleague_tool_call([{"name": "say", "arguments": {}}]))

    def test_preferred_colleague_recovery_tool_uses_generic_delegate(self) -> None:
        self.assertEqual(
            preferred_colleague_tool_name(
                [
                    {"name": "create_flowhunt_project_issue"},
                    {"name": "invoke_flowhunt_flow"},
                    {"name": "delegate_to_subagent"},
                ]
            ),
            "delegate_to_subagent",
        )

    def test_preferred_colleague_recovery_tool_falls_back_to_flow_invoke(self) -> None:
        self.assertEqual(
            preferred_colleague_tool_name(
                [
                    {"name": "create_flowhunt_project_issue"},
                    {"name": "invoke_flowhunt_flow"},
                ]
            ),
            "invoke_flowhunt_flow",
        )

    def test_colleague_tool_recovery_parser_accepts_wrapped_json(self) -> None:
        parsed = parse_colleague_tool_recovery('Sure:\n{"delegate": true, "message": "Check status"}')

        self.assertEqual(parsed["message"], "Check status")

    def test_missing_colleague_tool_recovery_creates_flow_tool_call(self) -> None:
        def provider(client, model, prompt, timeout, max_output_tokens, tools):
            self.assertIn("When was the last incident?", prompt)
            return '{"delegate": true, "message": "Check the LiveAgent status archive for the latest incident."}', []

        registry = AgentProviderRegistry()
        registry.register("test", provider)

        calls = recover_missing_colleague_tool_call(
            object(),
            registry,
            self.make_config(),
            {"id": 42, "call_id": "call-1", "data": {"text": "When was the last incident?"}},
            {"summary": "The caller is asking about LiveAgent status incidents.", "events": []},
            "I'll check the latest incident status for you.",
            [{"name": "invoke_flowhunt_flow"}],
        )

        self.assertEqual(calls[0]["name"], "invoke_flowhunt_flow")
        self.assertEqual(calls[0]["arguments"]["call_id"], "call-1")
        self.assertEqual(calls[0]["arguments"]["response_to_event_id"], 42)
        self.assertIn("LiveAgent status archive", calls[0]["arguments"]["message"])


if __name__ == "__main__":
    unittest.main()
