from __future__ import annotations

import unittest

from voicebot.execution_model import (
    ExecutionIds,
    ExecutionScope,
    FrameOrderingKey,
    frame_category,
    frame_is_cancellation,
    frame_is_session_ordered,
    ids_from_frame,
    scope_from_frame,
    sort_frames_for_session,
)
from voicebot.events import EventStore
from voicebot.frame_events import frame_event_mapping_issues
from voicebot.frames import ControlFrame, TextFrame, TranscriptionFrame


class ExecutionModelTests(unittest.TestCase):
    def test_scope_from_frame_uses_workspace_voicebot_and_session_metadata(self) -> None:
        frame = TextFrame(
            "agent_request",
            "call-1",
            "hello",
            data={
                "workspace_id": "workspace-1",
                "voicebot_id": "voicebot-1",
                "session_id": "session-1",
            },
        )

        self.assertEqual(
            scope_from_frame(frame),
            ExecutionScope(
                workspace_id="workspace-1",
                voicebot_id="voicebot-1",
                session_id="session-1",
                call_id="call-1",
            ),
        )

    def test_scope_from_frame_falls_back_to_call_id_as_session_id(self) -> None:
        frame = TextFrame("system", "call-1", "hello")

        self.assertEqual(scope_from_frame(frame).session_id, "call-1")

    def test_scope_can_require_workspace_voicebot_and_session(self) -> None:
        with self.assertRaisesRegex(ValueError, "workspace_id"):
            ExecutionScope().require_workspace()
        with self.assertRaisesRegex(ValueError, "voicebot_id"):
            ExecutionScope(workspace_id="workspace-1", session_id="session-1").require_workspace()
        with self.assertRaisesRegex(ValueError, "session_id"):
            ExecutionScope(workspace_id="workspace-1", voicebot_id="voicebot-1").require_workspace()

    def test_scope_matches_same_workspace_voicebot_and_session(self) -> None:
        scope = ExecutionScope("workspace-1", "voicebot-1", "session-1", "call-1")

        self.assertTrue(scope.same_session(ExecutionScope("workspace-1", "voicebot-1", "session-1", "call-2")))
        self.assertFalse(scope.same_session(ExecutionScope("workspace-1", "voicebot-2", "session-1", "call-1")))
        self.assertFalse(ExecutionScope(session_id="session-1").same_session(scope))

    def test_event_store_append_scoped_adds_scope_and_execution_ids(self) -> None:
        events = EventStore(max_context_events=20)

        event = events.append_scoped(
            ExecutionScope(
                workspace_id="workspace-1",
                voicebot_id="voicebot-1",
                session_id="session-1",
                call_id="call-1",
            ),
            "user_transcript",
            {"text": "hello"},
            ExecutionIds(turn_id=2, trace_id="trace-1"),
        )

        self.assertEqual(event.call_id, "call-1")
        self.assertEqual(event.data["workspace_id"], "workspace-1")
        self.assertEqual(event.data["voicebot_id"], "voicebot-1")
        self.assertEqual(event.data["session_id"], "session-1")
        self.assertEqual(event.data["turn_id"], 2)
        self.assertEqual(event.data["trace_id"], "trace-1")
        self.assertEqual(event.data["text"], "hello")

    def test_event_store_append_scoped_preserves_canonical_scope_and_ids(self) -> None:
        events = EventStore(max_context_events=20)

        event = events.append_scoped(
            ExecutionScope("workspace-1", "voicebot-1", "session-1", "call-1"),
            "user_transcript",
            {
                "workspace_id": "payload-workspace",
                "voicebot_id": "payload-voicebot",
                "session_id": "payload-session",
                "trace_id": "payload-trace",
                "text": "hello",
            },
            ExecutionIds(trace_id="trace-1"),
        )

        self.assertEqual(event.data["workspace_id"], "workspace-1")
        self.assertEqual(event.data["voicebot_id"], "voicebot-1")
        self.assertEqual(event.data["session_id"], "session-1")
        self.assertEqual(event.data["trace_id"], "trace-1")
        self.assertEqual(event.data["text"], "hello")

    def test_ids_from_frame_extracts_turn_request_and_external_task_ids(self) -> None:
        frame = TranscriptionFrame(
            "user_transcript",
            "call-1",
            7,
            text="hello",
            trace_id="trace-1",
            data={"response_to_event_id": 12, "task_id": "task-1"},
        )

        ids = ids_from_frame(frame)

        self.assertEqual(ids.frame_id, frame.frame_id)
        self.assertEqual(ids.turn_id, 7)
        self.assertEqual(ids.response_to_event_id, 12)
        self.assertEqual(ids.external_task_id, "task-1")
        self.assertEqual(ids.trace_id, "trace-1")

    def test_frame_categories_and_ordering_are_stable(self) -> None:
        self.assertEqual(frame_category("audio_input"), "audio")
        self.assertEqual(frame_category("agent_response"), "agent")
        self.assertEqual(frame_category("call_control_completed"), "call_control")
        self.assertEqual(frame_category("unknown"), "system")
        self.assertTrue(frame_is_session_ordered(TextFrame("agent_response", "call-1", "ok")))
        self.assertFalse(frame_is_session_ordered(TextFrame("system", "call-1", "ok")))

    def test_cancellation_frames_are_identified(self) -> None:
        self.assertTrue(frame_is_cancellation(ControlFrame("cancel_tts", "call-1", reason="barge_in")))
        self.assertFalse(frame_is_cancellation(TextFrame("agent_response", "call-1", "ok")))

    def test_frame_ordering_key_sorts_by_session_turn_timestamp_and_frame_id(self) -> None:
        later = TranscriptionFrame(
            "user_transcript",
            "call-1",
            2,
            text="second",
            data={"session_id": "session-1"},
        )
        earlier = TranscriptionFrame(
            "user_transcript",
            "call-1",
            1,
            text="first",
            data={"session_id": "session-1"},
        )

        ordered = sort_frames_for_session([later, earlier])

        self.assertEqual([frame.data["text"] for frame in ordered], ["first", "second"])
        self.assertEqual(FrameOrderingKey.from_frame(earlier).to_data()["session_id"], "session-1")

    def test_frame_event_mapping_covers_persistable_frames(self) -> None:
        self.assertEqual(frame_event_mapping_issues(), [])

    def test_frame_event_mapping_reports_invalid_entries(self) -> None:
        issues = frame_event_mapping_issues(
            {
                "agent_response": "agent_response_received",
                "unknown_frame": "system",
                "system": "unknown_event",
            }
        )

        issue_names = {issue["issue"] for issue in issues}

        self.assertIn("frame kind is not declared", issue_names)
        self.assertIn("event type is not declared", issue_names)
        self.assertIn("persistable frame kind missing event mapping", issue_names)


if __name__ == "__main__":
    unittest.main()
