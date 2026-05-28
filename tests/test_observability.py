from __future__ import annotations

import unittest

from voicebot.events import EventStore
from voicebot.observability import (
    ConversationExpectation,
    TraceContext,
    build_timeline,
    evaluate_conversation,
    provider_observability_summary,
    structured_log_record,
)


class ObservabilityTests(unittest.TestCase):
    def test_trace_context_extracts_debug_fields_from_event(self) -> None:
        events = EventStore(max_context_events=20)
        event = events.append(
            "call-1",
            "user_transcript",
            {
                "trace_id": "trace-1",
                "workspace_id": "workspace-1",
                "voicebot_id": "voicebot-1",
                "session_id": "session-1",
                "turn_id": 3,
                "text": "hello",
            },
        )

        context = TraceContext.from_event(event)

        self.assertEqual(context.trace_id, "trace-1")
        self.assertEqual(context.workspace_id, "workspace-1")
        self.assertEqual(context.voicebot_id, "voicebot-1")
        self.assertEqual(context.session_id, "session-1")
        self.assertEqual(context.call_id, "call-1")
        self.assertEqual(context.turn_id, 3)
        self.assertEqual(context.event_id, event.id)

    def test_structured_log_record_includes_trace_fields(self) -> None:
        context = TraceContext(
            trace_id="trace-1",
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            session_id="session-1",
            call_id="call-1",
            turn_id=2,
            event_id=7,
        )

        record = structured_log_record("INFO", "stt completed", context, provider="openai")

        self.assertEqual(record["level"], "info")
        self.assertEqual(record["trace_id"], "trace-1")
        self.assertEqual(record["workspace_id"], "workspace-1")
        self.assertEqual(record["provider"], "openai")

    def test_timeline_groups_events_by_debug_category(self) -> None:
        events = EventStore(max_context_events=20)
        events.append("call-1", "call_started", {"workspace_id": "workspace-1"})
        events.append("call-1", "user_speech_started", {"turn_id": 1})
        events.append("call-1", "stt_started", {"turn_id": 1})
        events.append("call-1", "agent_response_received", {"text": "Hi"})
        events.append("call-1", "bot_playback_started", {})

        timeline = build_timeline(events.list_events(call_id="call-1"))

        self.assertEqual(timeline["counts"]["call"], 1)
        self.assertEqual(timeline["counts"]["caller_audio"], 1)
        self.assertEqual(timeline["counts"]["stt"], 1)
        self.assertEqual(timeline["counts"]["agent"], 1)
        self.assertEqual(timeline["counts"]["playback"], 1)
        self.assertEqual([entry["id"] for entry in timeline["events"]], sorted(entry["id"] for entry in timeline["events"]))

    def test_provider_summary_reports_latency_and_failures(self) -> None:
        events = EventStore(max_context_events=20)
        events.append("call-1", "metrics", {"name": "stt_duration_seconds", "value": 0.2, "provider": "openai"})
        events.append("call-1", "metrics", {"name": "stt_duration_seconds", "value": 0.4, "provider": "openai"})
        events.append("call-1", "tts_failed", {"provider": "openai", "error": "bad request"})
        events.append("call-1", "subagent_task_failed", {"provider": "flowhunt_flow", "error": "pending forever"})

        summary = provider_observability_summary(events.list_events(call_id="call-1"))

        self.assertEqual(summary["providers"]["openai"]["latency_count"], 2)
        self.assertEqual(summary["providers"]["openai"]["latency_avg"], 0.30000000000000004)
        self.assertEqual(summary["providers"]["openai"]["failure_count"], 1)
        self.assertEqual(summary["providers"]["flowhunt_flow"]["failure_count"], 1)

    def test_conversation_evaluator_detects_missing_events_and_duplicate_responses(self) -> None:
        events = EventStore(max_context_events=20)
        events.append("call-1", "user_transcript", {"text": "Question"})
        events.append("call-1", "agent_response_received", {"text": "Same answer"})
        events.append("call-1", "agent_response_received", {"text": "Same answer"})

        result = evaluate_conversation(
            events.list_events(call_id="call-1"),
            ConversationExpectation(
                must_include_event_types=("call_connected", "user_transcript"),
                max_duplicate_agent_responses=1,
                require_final_agent_response=True,
            ),
        )

        self.assertFalse(result["ok"])
        self.assertIn("missing event type: call_connected", result["failures"])
        self.assertIn("duplicate agent response repeated 2 times", result["failures"])

    def test_conversation_evaluator_passes_expected_sequence(self) -> None:
        events = EventStore(max_context_events=20)
        events.append("call-1", "call_connected", {})
        events.append("call-1", "user_transcript", {"text": "Question"})
        events.append("call-1", "agent_response_received", {"text": "Answer"})

        result = evaluate_conversation(
            events.list_events(call_id="call-1"),
            ConversationExpectation(
                must_include_event_types=("call_connected", "user_transcript"),
                require_final_agent_response=True,
            ),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["failures"], [])


if __name__ == "__main__":
    unittest.main()
