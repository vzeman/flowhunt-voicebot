from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, _seconds_between_timestamps, create_app
from voicebot.calls import CallRegistry
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.subagents import SubagentCoordinator, SubagentTask, SubagentTaskRequest, SubagentTaskResult, SubagentTaskStore
from voicebot.transcripts import TranscriptStore


class FakeAsterisk:
    def hangup(self, call_id: str):
        return FakeControlResult(True, f"hung up {call_id}")

    def transfer(self, call_id: str, target: str):
        return FakeControlResult(True, f"transferred {call_id} to {target}")

    def send_dtmf(self, call_id: str, digit: str):
        return FakeControlResult(True, f"sent DTMF {digit} to {call_id}")


class BrokenAsterisk(FakeAsterisk):
    def hangup(self, call_id: str):
        raise OSError("AMI unavailable")


class FakeControlResult:
    def __init__(self, ok: bool, message: str) -> None:
        self.ok = ok
        self.message = message


class FakeWebRTCSession:
    def __init__(self, call_id: str) -> None:
        self.call_id = call_id

    def snapshot(self):
        return {"call_id": self.call_id, "transport": "webrtc", "session_id": "session-1"}


class SlowProgressSession:
    def __init__(self, call_id: str, delay: float = 0.4) -> None:
        self.call_id = call_id
        self.delay = delay
        self.responses = []

    def snapshot(self):
        return {"call_id": self.call_id, "transport": "webrtc", "session_id": "session-1"}

    def submit_agent_response(self, response):
        time.sleep(self.delay)
        self.responses.append(response)


class RecordingResponseSession:
    def __init__(self, call_id: str, events: EventStore) -> None:
        self.call_id = call_id
        self.events = events
        self.responses = []

    def snapshot(self):
        return {"call_id": self.call_id, "transport": "webrtc", "session_id": "session-1"}

    def submit_agent_response(self, response):
        self.responses.append(response)
        return self.events.append(
            self.call_id,
            "agent_response_partial" if response.partial else "agent_response_received",
            {
                "text": response.text,
                "response_to_event_id": response.response_to_event_id,
                "partial": response.partial,
            },
        )


class FakeWebRTCManager:
    def __init__(self) -> None:
        self.closed_calls = []

    async def close_call(self, call_id: str) -> bool:
        self.closed_calls.append(call_id)
        return True


class FakeFlowHuntResult:
    ok = True
    message = "The FlowHunt project team checked it and returned a result."
    data = {"response": {"id": "issue-1", "status": "completed", "result": message}}


class FakeFlowHuntClient:
    calls = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def create_project_issue(self, *args):
        self.calls.append((self.kwargs, args))
        return FakeFlowHuntResult()

    def get_project_issue(self, *args):
        self.calls.append((self.kwargs, args))
        return FakeFlowHuntResult()

    def invoke_flow_and_wait(self, *args):
        self.calls.append((self.kwargs, args))
        return FakeFlowHuntResult()


class FakeSubagentProvider:
    kind = "flowhunt_flow"

    def __init__(self) -> None:
        self.requests = []

    def submit(self, request: SubagentTaskRequest) -> SubagentTask:
        self.requests.append(request)
        task, _created = SubagentTaskStore().get_or_create_requested(request)
        return task.with_status("running", external_task_id="task-1")

    def poll(self, task: SubagentTask) -> SubagentTask:
        return task.with_status("completed", result=SubagentTaskResult(summary="The answer is 42."))

    def cancel(self, task: SubagentTask) -> SubagentTask:
        return task.with_status("cancelled")


class ApiCallControlTests(unittest.TestCase):
    def test_seconds_between_timestamps_handles_valid_invalid_and_reversed_values(self) -> None:
        self.assertEqual(
            _seconds_between_timestamps("2026-05-28T00:00:00+00:00", "2026-05-28T00:00:02.500000+00:00"),
            2.5,
        )
        self.assertEqual(
            _seconds_between_timestamps("2026-05-28T00:00:03Z", "2026-05-28T00:00:02Z"),
            0.0,
        )
        self.assertIsNone(_seconds_between_timestamps("not-a-time", "2026-05-28T00:00:02Z"))

    def build_client(
        self,
        asterisk=None,
        registry: CallRegistry | None = None,
        webrtc=None,
        settings: Settings | None = None,
        subagent_coordinator: SubagentCoordinator | None = None,
    ) -> tuple[TestClient, EventStore, AgentTaskTracker]:
        events = EventStore(max_context_events=20)
        if subagent_coordinator is not None and subagent_coordinator.events is None:
            subagent_coordinator.events = events
        tracker = AgentTaskTracker()
        app = create_app(
            events,
            registry or CallRegistry(),
            tracker,
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            asterisk,
            settings=settings,
            webrtc=webrtc,
            subagent_coordinator=subagent_coordinator,
        )
        return TestClient(app), events, tracker

    def assert_call_control_event_sequence(self, persisted, action: str):
        self.assertEqual(
            [event.type for event in persisted],
            ["call_control_requested", "security_audit", "call_control_completed"],
        )
        self.assertEqual(persisted[1].data["action"], f"call_control.{action}")
        self.assertEqual(persisted[1].data["resource_type"], "call")
        self.assertEqual(persisted[1].data["resource_id"], "call-1")

    def test_call_control_records_failure_when_ami_is_not_configured(self) -> None:
        client, events, tracker = self.build_client(asterisk=None)

        response = client.post(
            "/calls/call-1/control",
            json={"action": "hangup", "response_to_event_id": 42},
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn(42, tracker.responded_event_ids)
        persisted = events.list_events(call_id="call-1")
        self.assert_call_control_event_sequence(persisted, "hangup")
        self.assertFalse(persisted[2].data["ok"])
        self.assertEqual(persisted[2].data["message"], "Asterisk AMI control is not configured")

    def test_partial_agent_response_does_not_mark_task_responded_until_finalize(self) -> None:
        registry = CallRegistry()
        client, events, tracker = self.build_client(registry=registry)
        session = RecordingResponseSession("call-1", events)
        registry.add(session)

        partial = client.post(
            "/calls/call-1/responses",
            json={"text": "Hello.", "response_to_event_id": 42, "response_kind": "stream_chunk", "partial": True},
        )
        self.assertEqual(partial.status_code, 200)
        self.assertNotIn(42, tracker.responded_event_ids)

        final = client.post(
            "/calls/call-1/responses",
            json={"text": "", "response_to_event_id": 42, "response_kind": "stream_finalized", "finalize_only": True},
        )

        self.assertEqual(final.status_code, 200)
        self.assertIn(42, tracker.responded_event_ids)
        self.assertEqual(len(session.responses), 1)
        self.assertTrue(session.responses[0].partial)
        persisted = events.list_events(call_id="call-1")
        self.assertEqual(persisted[-1].data["stream_finalized"], True)

    def test_call_control_records_failure_when_transfer_target_is_missing(self) -> None:
        client, events, tracker = self.build_client(asterisk=FakeAsterisk())

        response = client.post(
            "/calls/call-1/control",
            json={"action": "transfer", "response_to_event_id": 43},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(43, tracker.responded_event_ids)
        persisted = events.list_events(call_id="call-1")
        self.assert_call_control_event_sequence(persisted, "transfer")
        self.assertFalse(persisted[2].data["ok"])
        self.assertEqual(persisted[2].data["message"], "transfer requires target")

    def test_call_control_records_successful_result(self) -> None:
        client, events, tracker = self.build_client(asterisk=FakeAsterisk())

        response = client.post(
            "/calls/call-1/control",
            json={"action": "transfer", "target": " 123 ", "response_to_event_id": 44},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(44, tracker.responded_event_ids)
        persisted = events.list_events(call_id="call-1")
        self.assert_call_control_event_sequence(persisted, "transfer")
        self.assertTrue(persisted[2].data["ok"])
        self.assertEqual(persisted[2].data["message"], "transferred call-1 to 123")

    def test_call_control_records_ami_exception_as_failed_result(self) -> None:
        client, events, tracker = self.build_client(asterisk=BrokenAsterisk())

        response = client.post(
            "/calls/call-1/control",
            json={"action": "hangup", "response_to_event_id": 49},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(49, tracker.responded_event_ids)
        persisted = events.list_events(call_id="call-1")
        self.assert_call_control_event_sequence(persisted, "hangup")
        self.assertFalse(persisted[2].data["ok"])
        self.assertIn("Asterisk AMI request failed", persisted[2].data["message"])

    def test_webrtc_hangup_closes_webrtc_session(self) -> None:
        registry = CallRegistry()
        registry.add(FakeWebRTCSession("call-1"))
        manager = FakeWebRTCManager()
        client, events, tracker = self.build_client(asterisk=FakeAsterisk(), registry=registry, webrtc=manager)

        response = client.post(
            "/calls/call-1/control",
            json={"action": "hangup", "response_to_event_id": 50},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(manager.closed_calls, ["call-1"])
        self.assertIn(50, tracker.responded_event_ids)
        completed = events.list_events(call_id="call-1")[-1]
        self.assertTrue(completed.data["ok"])
        self.assertEqual(completed.data["message"], "WebRTC call closed")

    def test_transfer_rejects_control_characters_in_target(self) -> None:
        client, events, tracker = self.build_client(asterisk=FakeAsterisk())

        response = client.post(
            "/calls/call-1/control",
            json={"action": "transfer", "target": "123\r\nAction: Hangup", "response_to_event_id": 48},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "transfer target must not contain control characters")
        self.assertNotIn(48, tracker.responded_event_ids)
        persisted = events.list_events(call_id="call-1")
        self.assertEqual([event.type for event in persisted], ["call_control_requested", "security_audit"])
        self.assertEqual(persisted[1].data["action"], "call_control.transfer")

    def test_call_control_records_send_dtmf_result(self) -> None:
        client, events, tracker = self.build_client(asterisk=FakeAsterisk())

        response = client.post(
            "/calls/call-1/control",
            json={"action": "send_dtmf", "digit": "1", "response_to_event_id": 45},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(45, tracker.responded_event_ids)
        persisted = events.list_events(call_id="call-1")
        self.assert_call_control_event_sequence(persisted, "send_dtmf")
        self.assertTrue(persisted[2].data["ok"])
        self.assertEqual(persisted[2].data["message"], "sent DTMF 1 to call-1")

    def test_send_dtmf_tool_requires_digit(self) -> None:
        client, events, tracker = self.build_client(asterisk=FakeAsterisk())

        response = client.post(
            "/agent/tools/send_dtmf",
            json={"arguments": {"call_id": "call-1", "response_to_event_id": 46}},
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn(46, tracker.responded_event_ids)
        self.assertEqual(events.list_events(call_id="call-1"), [])

    def test_send_dtmf_rejects_invalid_digit(self) -> None:
        client, events, tracker = self.build_client(asterisk=FakeAsterisk())

        response = client.post(
            "/calls/call-1/control",
            json={"action": "send_dtmf", "digit": "12", "response_to_event_id": 47},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "digit must be one DTMF character: 0-9, *, #, A-D")
        self.assertNotIn(47, tracker.responded_event_ids)
        persisted = events.list_events(call_id="call-1")
        self.assertEqual([event.type for event in persisted], ["call_control_requested", "security_audit"])
        self.assertEqual(persisted[1].data["action"], "call_control.send_dtmf")

    def test_send_dtmf_normalizes_letter_digit(self) -> None:
        client, events, _tracker = self.build_client(asterisk=FakeAsterisk())

        response = client.post(
            "/calls/call-1/control",
            json={"action": "send_dtmf", "digit": "a"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(events.list_events(call_id="call-1")[-1].data["message"], "sent DTMF A to call-1")

    def test_flowhunt_project_issue_tool_records_result(self) -> None:
        FakeFlowHuntClient.calls = []
        settings = Settings(
            flowhunt_api_key="key",
            flowhunt_workspace_id="workspace-1",
            flowhunt_project_id="project-1",
            flowhunt_complex_backend="project",
            flowhunt_issue_wait_seconds=0.1,
            flowhunt_issue_poll_interval_seconds=0.1,
        )
        client, events, tracker = self.build_client(settings=settings)

        with patch("voicebot.api.FlowHuntClient", FakeFlowHuntClient):
            response = client.post(
                "/agent/tools/create_flowhunt_project_issue",
                json={
                    "arguments": {
                        "call_id": "call-1",
                        "title": "Check website",
                        "description": "Review the caller website.",
                        "response_to_event_id": 51,
                    }
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], FakeFlowHuntResult.message)
        self.assertEqual([event.type for event in events.list_events(call_id="call-1")], [
            "flowhunt_issue_created",
            "flowhunt_issue_completed",
        ])
        self.assertEqual(FakeFlowHuntClient.calls[0][1][0], "project-1")

    def test_flowhunt_project_issue_tool_does_not_use_english_vague_topic_filter(self) -> None:
        FakeFlowHuntClient.calls = []
        settings = Settings(
            flowhunt_api_key="key",
            flowhunt_workspace_id="workspace-1",
            flowhunt_project_id="project-1",
            flowhunt_complex_backend="project",
        )
        client, events, tracker = self.build_client(settings=settings)

        with patch("voicebot.api.FlowHuntClient", FakeFlowHuntClient):
            response = client.post(
                "/agent/tools/create_flowhunt_project_issue",
                json={
                    "arguments": {
                        "call_id": "call-1",
                        "title": "General Support Request from Caller Mentioning Various Technologies",
                        "description": (
                            "Caller mentioned a range of topics including FlowHunt, LiveAgent, SIP trunk, "
                            "Asterisk, WebRTC, OpenAI, Anthropic, Viktor, project IDs, extensions, and support ticket details."
                        ),
                    }
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(FakeFlowHuntClient.calls[0][1][0], "project-1")
        self.assertEqual([event.type for event in events.list_events(call_id="call-1")], [
            "flowhunt_issue_created",
            "flowhunt_issue_completed",
        ])

    def test_flowhunt_flow_tool_records_result(self) -> None:
        FakeFlowHuntClient.calls = []
        settings = Settings(
            flowhunt_api_key="key",
            flowhunt_workspace_id="workspace-1",
            flowhunt_flow_id="flow-1",
            flowhunt_flow_wait_seconds=0.1,
            flowhunt_flow_poll_interval_seconds=0.1,
        )
        client, events, tracker = self.build_client(settings=settings)

        with patch("voicebot.api.FlowHuntClient", FakeFlowHuntClient):
            response = client.post(
                "/agent/tools/invoke_flowhunt_flow",
                json={
                    "arguments": {
                        "call_id": "call-1",
                        "message": "Review the caller website.",
                        "response_to_event_id": 52,
                    }
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], FakeFlowHuntResult.message)
        self.assertEqual(
            [event.type for event in events.list_events(call_id="call-1")],
            ["flowhunt_flow_invoked", "flowhunt_flow_completed"],
        )
        self.assertEqual(FakeFlowHuntClient.calls[0][1][0], "flow-1")
        self.assertIn(52, tracker.snapshot()["responded_event_ids"])

    def test_flowhunt_flow_tool_is_idempotent_for_same_request_event(self) -> None:
        FakeFlowHuntClient.calls = []
        settings = Settings(
            flowhunt_api_key="key",
            flowhunt_workspace_id="workspace-1",
            flowhunt_flow_id="flow-1",
            flowhunt_flow_wait_seconds=0.1,
            flowhunt_flow_poll_interval_seconds=0.1,
        )
        client, events, _tracker = self.build_client(settings=settings)

        with patch("voicebot.api.FlowHuntClient", FakeFlowHuntClient):
            first = client.post(
                "/agent/tools/invoke_flowhunt_flow",
                json={
                    "arguments": {
                        "call_id": "call-1",
                        "message": "Review the caller website.",
                        "response_to_event_id": 52,
                    }
                },
            )
            second = client.post(
                "/agent/tools/invoke_flowhunt_flow",
                json={
                    "arguments": {
                        "call_id": "call-1",
                        "message": "Review the caller website again.",
                        "response_to_event_id": 52,
                    }
                },
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(len(FakeFlowHuntClient.calls), 1)
        self.assertEqual(
            [event.type for event in events.list_events(call_id="call-1")],
            ["flowhunt_flow_invoked", "flowhunt_flow_completed"],
        )

    def test_project_tool_routes_to_flow_when_configured(self) -> None:
        FakeFlowHuntClient.calls = []
        settings = Settings(
            flowhunt_api_key="key",
            flowhunt_workspace_id="workspace-1",
            flowhunt_flow_id="flow-1",
            flowhunt_complex_backend="flow",
        )
        client, events, _tracker = self.build_client(settings=settings)

        with patch("voicebot.api.FlowHuntClient", FakeFlowHuntClient):
            response = client.post(
                "/agent/tools/create_flowhunt_project_issue",
                json={
                    "arguments": {
                        "call_id": "call-1",
                        "title": "Check website",
                        "description": "Review the caller website.",
                    }
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [event.type for event in events.list_events(call_id="call-1")],
            ["flowhunt_flow_invoked", "flowhunt_flow_completed"],
        )
        self.assertEqual(FakeFlowHuntClient.calls[0][1][0], "flow-1")

    def test_flowhunt_flow_tool_can_submit_generic_subagent_task(self) -> None:
        provider = FakeSubagentProvider()
        coordinator = SubagentCoordinator()
        coordinator.register(provider)
        settings = Settings(
            flowhunt_api_key="key",
            flowhunt_workspace_id="workspace-1",
            flowhunt_flow_id="flow-1",
            subagent_task_initial_poll_seconds=0.1,
        )
        client, events, tracker = self.build_client(settings=settings, subagent_coordinator=coordinator)

        response = client.post(
            "/agent/tools/invoke_flowhunt_flow",
            json={
                "arguments": {
                    "call_id": "call-1",
                    "message": "Review the caller website.",
                    "response_to_event_id": 53,
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task"]["status"], "running")
        self.assertEqual(provider.requests[0].metadata["flow_id"], "flow-1")
        self.assertEqual(
            [event.type for event in events.list_events(call_id="call-1")],
            ["flowhunt_flow_invoked", "subagent_task_requested", "subagent_task_updated"],
        )
        self.assertIn(53, tracker.snapshot()["responded_event_ids"])
        tasks = client.get("/subagent/tasks?workspace_id=workspace-1").json()["tasks"]
        self.assertEqual(tasks[0]["external_task_id"], "task-1")

    def test_flowhunt_flow_tool_schedules_work_without_waiting_for_progress_speech(self) -> None:
        provider = FakeSubagentProvider()
        coordinator = SubagentCoordinator()
        coordinator.register(provider)
        registry = CallRegistry()
        slow_session = SlowProgressSession("call-1", delay=0.4)
        registry.add(slow_session)
        settings = Settings(
            flowhunt_api_key="key",
            flowhunt_workspace_id="workspace-1",
            flowhunt_flow_id="flow-1",
            subagent_task_initial_poll_seconds=0.1,
        )
        client, _events, tracker = self.build_client(
            registry=registry,
            settings=settings,
            subagent_coordinator=coordinator,
        )

        started = time.monotonic()
        response = client.post(
            "/agent/tools/invoke_flowhunt_flow",
            json={
                "arguments": {
                    "call_id": "call-1",
                    "message": "Review the caller website.",
                    "response_to_event_id": 54,
                }
            },
        )
        elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 200)
        self.assertLess(elapsed, 0.3)
        self.assertEqual(provider.requests[0].input_text, "Review the caller website.")
        self.assertIn(54, tracker.snapshot()["responded_event_ids"])


if __name__ == "__main__":
    unittest.main()
