from __future__ import annotations

import tempfile
import time
import unittest

import numpy as np
from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.audio import STT_SAMPLE_RATE
from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.events import EventStore, VoicebotEvent
from voicebot.subagents import SubagentCoordinator, SubagentTask, SubagentTaskRequest, SubagentTaskResult, SubagentTaskStore
from voicebot.transcripts import TranscriptStore
from voicebot.webrtc import WebRTCCallSession


class FakeTranscriptionResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.reason = None
        self.metadata = {"provider": "fake-full-integration"}
        self.is_final = True


class RecordingSTT:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[int, int]] = []

    def transcribe(self, audio, sample_rate=8000):
        self.calls.append((len(audio), sample_rate))
        return FakeTranscriptionResult(self.text)

    def transcribe_stream(self, audio, sample_rate=8000):
        yield self.transcribe(audio, sample_rate)


class RecordingTTS:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def synthesize(self, text: str):
        self.texts.append(text)
        return np.ones(160, dtype=np.float32) * 0.2, 0.01

    def synthesize_stream(self, text: str):
        yield self.synthesize(text)


class CompletingSubagentProvider:
    kind = "flowhunt_flow"

    def __init__(self) -> None:
        self.requests: list[SubagentTaskRequest] = []
        self.polls: list[str] = []

    def submit(self, request: SubagentTaskRequest) -> SubagentTask:
        self.requests.append(request)
        task, _created = SubagentTaskStore().get_or_create_requested(request)
        return task.with_status("running", external_task_id="flow-task-1", progress_message="Specialist started.")

    def poll(self, task: SubagentTask) -> SubagentTask:
        self.polls.append(task.task_id)
        return task.with_status(
            "completed",
            result=SubagentTaskResult(
                summary="voice_summary: The specialist confirmed LiveAgent manages support tickets.",
                content=(
                    "chat_details: LiveAgent centralizes customer conversations, ticket ownership, "
                    "status tracking, and team collaboration."
                ),
                context={"confidence": "high"},
            ),
        )

    def cancel(self, task: SubagentTask) -> SubagentTask:
        return task.with_status("cancelled")


class FullIntegrationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_voice_question_workflow_records_stt_tts_tasks_and_rich_chat_answer(self) -> None:
        harness = self.build_voice_harness("What is LiveAgent?")
        with harness.client:
            self.drive_voice_turn(harness.session)
            request = self.wait_for_request(harness.events, harness.session.call_id)

            tasks = harness.client.get(f"/agent/tasks?call_id={harness.session.call_id}&limit=10")
            self.assertEqual(tasks.status_code, 200)
            self.assertEqual(tasks.json()["pending"][0]["id"], request.id)

            response = harness.client.post(
                f"/calls/{harness.session.call_id}/responses",
                json={
                    "text": "LiveAgent helps teams manage customer support tickets.",
                    "response_to_event_id": request.id,
                    "chat": {
                        "text": (
                            "LiveAgent is customer support software for collecting conversations into tickets, "
                            "assigning them to agents, tracking status, and keeping the support history visible."
                        ),
                        "blocks": [
                            {"type": "bullet", "text": "Turns conversations into trackable tickets"},
                            {"type": "bullet", "text": "Shows assignment, status, and support history"},
                        ],
                    },
                },
            )

            self.assertEqual(response.status_code, 200)
            payload = response.json()["event"]
            self.assertEqual(payload["type"], "agent_response_received")
            self.assertNotEqual(payload["data"]["text"], payload["data"]["chat"]["text"])
            self.assertEqual(harness.tts.texts, ["LiveAgent helps teams manage customer support tickets."])
            self.assertEqual(harness.stt.calls[0][1], STT_SAMPLE_RATE)
            self.assertIn(request.id, harness.tracker.responded_event_ids)
            self.assert_events_include(
                harness.events,
                harness.session.call_id,
                [
                    "call_started",
                    "call_connected",
                    "user_speech_started",
                    "stt_started",
                    "stt_finished",
                    "user_transcript",
                    "agent_response_requested",
                    "agent_response_received",
                    "tts_started",
                    "agent_response_queued",
                    "tts_finished",
                ],
            )

    def test_hangup_workflow_speaks_ack_and_closes_webrtc_call(self) -> None:
        harness = self.build_voice_harness("Please hang up the call.")
        with harness.client:
            self.drive_voice_turn(harness.session)
            request = self.wait_for_request(harness.events, harness.session.call_id)

            spoken = harness.client.post(
                f"/calls/{harness.session.call_id}/responses",
                json={
                    "text": "Goodbye.",
                    "response_to_event_id": request.id,
                    "response_kind": "call_control_ack",
                },
            )
            self.assertEqual(spoken.status_code, 200)

            hangup = harness.client.post(
                "/agent/tools/hangup_call",
                json={"arguments": {"call_id": harness.session.call_id, "response_to_event_id": request.id}},
            )

            self.assertEqual(hangup.status_code, 200)
            self.assertTrue(hangup.json()["event"]["data"]["ok"])
            self.assertIsNone(harness.registry.get(harness.session.call_id))
            self.assertEqual(harness.tts.texts, ["Goodbye."])
            self.assert_events_include(
                harness.events,
                harness.session.call_id,
                [
                    "agent_response_received",
                    "tts_finished",
                    "call_control_requested",
                    "security_audit",
                    "call_ended",
                    "call_control_completed",
                ],
            )

    def test_subagent_workflow_requests_specialist_then_speaks_colleague_result_with_chat_detail(self) -> None:
        provider = CompletingSubagentProvider()
        coordinator = SubagentCoordinator()
        coordinator.register(provider)
        harness = self.build_voice_harness(
            "Can a specialist explain LiveAgent?",
            settings=Settings(
                greet_on_connect=False,
                call_recording_enabled=False,
                start_threshold=0.1,
                stop_threshold=0.05,
                vad_start_ms=0,
                silence_ms=20,
                min_seconds=0.35,
                webrtc_jitter_buffer_enabled=False,
                turn_coalesce_window_ms=0,
                flowhunt_workspace_id="workspace-1",
                flowhunt_flow_id="flow-1",
                subagent_task_initial_poll_seconds=0.05,
                subagent_task_poll_loop_seconds=0.05,
                subagent_task_max_poll_seconds=0.05,
            ),
            subagent_coordinator=coordinator,
        )
        with harness.client:
            self.drive_voice_turn(harness.session)
            request = self.wait_for_request(harness.events, harness.session.call_id)

            delegated = harness.client.post(
                "/agent/tools/invoke_flowhunt_flow",
                json={
                    "arguments": {
                        "call_id": harness.session.call_id,
                        "message": "Ask the LiveAgent specialist for a precise answer.",
                        "response_to_event_id": request.id,
                        "suppress_progress": True,
                    }
                },
            )
            self.assertEqual(delegated.status_code, 200)
            self.assertEqual(delegated.json()["task"]["status"], "running")

            colleague_request = self.wait_for_event(
                harness.events,
                harness.session.call_id,
                lambda event: event.type == "agent_response_requested"
                and event.data.get("reason") == "colleague_result",
                timeout=3.0,
            )
            final = harness.client.post(
                f"/calls/{harness.session.call_id}/responses",
                json={
                    "text": "The specialist confirmed LiveAgent manages support tickets and ownership.",
                    "response_to_event_id": colleague_request.id,
                    "response_kind": "colleague_result",
                    "chat": {
                        "text": (
                            "Specialist result: LiveAgent centralizes conversations into tickets, "
                            "tracks ownership and status, and gives teams a shared support history."
                        ),
                        "blocks": [
                            {"type": "bullet", "text": "Ticket ownership and status tracking"},
                            {"type": "bullet", "text": "Shared customer conversation history"},
                        ],
                    },
                },
            )

            self.assertEqual(final.status_code, 200)
            self.assertEqual(provider.requests[0].workspace_id, "workspace-1")
            self.assertIn("voice_summary", provider.requests[0].input_text)
            self.assertTrue(provider.polls)
            self.assertEqual(
                harness.tts.texts,
                ["The specialist confirmed LiveAgent manages support tickets and ownership."],
            )
            self.assertNotEqual(final.json()["event"]["data"]["text"], final.json()["event"]["data"]["chat"]["text"])
            self.assert_events_include(
                harness.events,
                harness.session.call_id,
                [
                    "flowhunt_flow_invoked",
                    "subagent_task_requested",
                    "subagent_task_updated",
                    "subagent_task_completed",
                    "agent_response_requested",
                    "agent_response_received",
                    "tts_finished",
                ],
            )

    def build_voice_harness(
        self,
        transcript_text: str,
        *,
        settings: Settings | None = None,
        subagent_coordinator: SubagentCoordinator | None = None,
    ):
        transcripts = TranscriptStore(self.directory.name)
        events = EventStore(max_context_events=100, transcript_store=transcripts)
        if subagent_coordinator is not None and subagent_coordinator.events is None:
            subagent_coordinator.events = events
        registry = CallRegistry()
        tracker = AgentTaskTracker()
        stt = RecordingSTT(transcript_text)
        tts = RecordingTTS()
        session = WebRTCCallSession(
            call_id=f"webrtc-{len(transcript_text)}",
            session_id="session-1",
            settings=settings
            or Settings(
                greet_on_connect=False,
                call_recording_enabled=False,
                start_threshold=0.1,
                stop_threshold=0.05,
                vad_start_ms=0,
                silence_ms=20,
                min_seconds=0.35,
                webrtc_jitter_buffer_enabled=False,
                turn_coalesce_window_ms=0,
            ),
            event_store=events,
            stt=stt,
            tts=tts,
            metadata={"workspace_id": "workspace-1", "voicebot_id": "voicebot-1", "channel_id": "widget-1"},
        )
        self.addCleanup(session.stop)
        session.start()
        registry.add(session)
        app = create_app(
            events,
            registry,
            tracker,
            WebSocketHub(),
            transcripts,
            None,
            settings=session.settings,
            subagent_coordinator=subagent_coordinator,
        )
        client = TestClient(app)
        return _Harness(client, events, registry, tracker, session, stt, tts)

    def drive_voice_turn(self, session: WebRTCCallSession) -> None:
        for _ in range(45):
            session.process_audio_block(np.full(160, 0.4, dtype=np.float32))
        for _ in range(3):
            session.process_audio_block(np.zeros(160, dtype=np.float32))

    def wait_for_request(self, events: EventStore, call_id: str) -> VoicebotEvent:
        return self.wait_for_event(
            events,
            call_id,
            lambda event: event.type == "agent_response_requested" and event.data.get("reason") != "call_connected",
        )

    def wait_for_event(self, events: EventStore, call_id: str, predicate, timeout: float = 2.0) -> VoicebotEvent:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in events.list_events(call_id=call_id):
                if predicate(event):
                    return event
            time.sleep(0.01)
        self.fail(f"timed out waiting for event in {call_id}")

    def assert_events_include(self, events: EventStore, call_id: str, expected_types: list[str]) -> None:
        actual = [event.type for event in events.list_events(call_id=call_id)]
        for event_type in expected_types:
            self.assertIn(event_type, actual)


class _Harness:
    def __init__(
        self,
        client: TestClient,
        events: EventStore,
        registry: CallRegistry,
        tracker: AgentTaskTracker,
        session: WebRTCCallSession,
        stt: RecordingSTT,
        tts: RecordingTTS,
    ) -> None:
        self.client = client
        self.events = events
        self.registry = registry
        self.tracker = tracker
        self.session = session
        self.stt = stt
        self.tts = tts


if __name__ == "__main__":
    unittest.main()
