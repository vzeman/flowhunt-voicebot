from __future__ import annotations

import threading
import time
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.runtime_config import VoicebotPromptConfig, VoicebotPromptConfigStore
from voicebot.storage.redis_agent_tasks import RedisAgentTaskTracker
from voicebot.subagents import SubagentCoordinator, SubagentTask, SubagentTaskRequest, SubagentTaskResult, SubagentTaskStore
from voicebot.transcripts import TranscriptStore


class FakeCallRegistry(CallRegistry):
    def __init__(self, active_call_ids: list[str]) -> None:
        super().__init__()
        self._active_call_ids = active_call_ids
        self._snapshots = {
            call_id: {
                "call_id": call_id,
                "route": {"workspace_id": "workspace-1", "voicebot_id": "voicebot-1"},
            }
            for call_id in active_call_ids
        }

    def active_call_ids(self) -> list[str]:
        return self._active_call_ids

    def snapshot(self, call_id: str) -> dict | None:
        return self._snapshots.get(call_id)


class CompletedSubagentProvider:
    kind = "flowhunt_flow"

    def submit(self, request: SubagentTaskRequest) -> SubagentTask:
        task, _created = SubagentTaskStore().get_or_create_requested(request)
        return task.with_status(
            "completed",
            result=SubagentTaskResult(
                summary="The speculative result is ready.",
                content="The website status is operational.",
            ),
        )

    def poll(self, task: SubagentTask) -> SubagentTask:
        return task

    def cancel(self, task: SubagentTask) -> SubagentTask:
        return task.with_status("cancelled")


class AgentTasksTests(unittest.TestCase):
    def build_client(self) -> tuple[TestClient, EventStore, AgentTaskTracker]:
        events = EventStore(max_context_events=20)
        tracker = AgentTaskTracker()
        app = create_app(
            events,
            FakeCallRegistry(["call-1", "call-2"]),
            tracker,
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        return TestClient(app), events, tracker

    def build_client_with_subagents(self, coordinator: SubagentCoordinator) -> tuple[TestClient, EventStore, AgentTaskTracker]:
        events = EventStore(max_context_events=50)
        tracker = AgentTaskTracker()
        app = create_app(
            events,
            FakeCallRegistry(["call-1", "call-2"]),
            tracker,
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
            subagent_coordinator=coordinator,
        )
        return TestClient(app), events, tracker

    def test_agent_tasks_filters_pending_events_by_call_id(self) -> None:
        client, events, tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "hello"})
        events.append("call-2", "agent_response_requested", {"text": "other"})
        events.append("inactive", "agent_response_requested", {"text": "ignored"})

        response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([event["id"] for event in payload["pending"]], [first.id])
        self.assertEqual([event["call_id"] for event in payload["context"]["events"]], ["call-1"])
        self.assertEqual(tracker.responded_event_ids, set())

    def test_agent_tasks_long_poll_waits_until_task_arrives(self) -> None:
        client, events, _tracker = self.build_client()
        payload: dict = {}

        def poll() -> None:
            payload.update(client.get("/agent/tasks?call_id=call-1&wait_seconds=1").json())

        thread = threading.Thread(target=poll)
        started = time.monotonic()
        thread.start()
        time.sleep(0.1)
        task = events.append("call-1", "agent_response_requested", {"text": "hello"})
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual([event["id"] for event in payload["pending"]], [task.id])

    def test_agent_tasks_include_cached_prompt_config_for_call_route(self) -> None:
        events = EventStore(max_context_events=20)
        prompts = VoicebotPromptConfigStore()
        prompts.save(
            "workspace-1",
            "voicebot-1",
            VoicebotPromptConfig(
                greeting="Pozdrav volajuceho.",
                filler_message="Chvíľku strpenia.",
                system_prompt="Use Slovak.",
                stt_prompt="LiveAgent",
                language="sk",
            ),
        )
        app = create_app(
            events,
            FakeCallRegistry(["call-1"]),
            AgentTaskTracker(),
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
            prompt_configs=prompts,
        )
        client = TestClient(app)
        events.append("call-1", "agent_response_requested", {"text": "hello"})

        response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["context"]["voicebot_prompts"]["language"], "sk")
        self.assertEqual(response.json()["pending"][0]["data"]["prompt_config"]["filler_message"], "Chvíľku strpenia.")
        self.assertEqual(
            response.json()["context"]["prompt_configs_by_call_id"]["call-1"]["system_prompt"],
            "Use Slovak.",
        )

    def test_agent_tasks_remember_detected_session_language_for_auto_prompt(self) -> None:
        client, events, _tracker = self.build_client()
        events.append("call-1", "user_transcript", {"turn_id": 1, "text": "Dobrý deň, rozprávate po slovensky?"})
        request = events.append("call-1", "agent_response_requested", {"text": "Dobrý deň, rozprávate po slovensky?"})

        response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pending"][0]["id"], request.id)
        self.assertEqual(payload["pending"][0]["data"]["session_language"]["language"], "sk")
        self.assertEqual(payload["context"]["voicebot_prompts"]["language"], "sk")
        self.assertEqual(payload["context"]["voicebot_prompts"]["language_source"], "session_detected")

    def test_agent_tasks_ignore_dropped_transcript_for_session_language(self) -> None:
        client, events, _tracker = self.build_client()
        transcript = events.append("call-1", "user_transcript", {"turn_id": 1, "text": "Dobrý deň, rozprávate po slovensky?"})
        events.append("call-1", "stt_result_dropped", {"transcript_event_id": transcript.id, "reason": "low_signal_transcript"})
        events.append("call-1", "agent_response_requested", {"text": "hello"})

        response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("session_language", response.json()["pending"][0]["data"])

    def test_agent_tasks_do_not_switch_language_on_short_greeting_fragment(self) -> None:
        client, events, _tracker = self.build_client()
        events.append("call-1", "stt_finished", {"turn_id": 1, "metadata": {"language": "en"}})
        events.append("call-1", "user_transcript", {"turn_id": 1, "text": "Can you help me?"})
        events.append("call-1", "user_transcript", {"turn_id": 2, "text": "Dobrý deň."})
        request = events.append("call-1", "agent_response_requested", {"text": "Dobrý deň."})

        response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pending"][0]["id"], request.id)
        self.assertEqual(payload["pending"][0]["data"]["session_language"]["language"], "en")
        self.assertEqual(payload["context"]["voicebot_prompts"]["language"], "en")

    def test_agent_tasks_applies_limit(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        events.append("call-1", "agent_response_requested", {"text": "second"})

        response = client.get("/agent/tasks?call_id=call-1&limit=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([event["id"] for event in response.json()["pending"]], [first.id])

    def test_agent_tasks_rejects_invalid_limit(self) -> None:
        client, _events, _tracker = self.build_client()

        response = client.get("/agent/tasks?limit=0")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "limit must be at least 1")

    def test_agent_tasks_omits_responded_events(self) -> None:
        client, events, tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        second = events.append("call-1", "agent_response_requested", {"text": "second"})
        tracker.mark_responded(first.id)

        response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([event["id"] for event in response.json()["pending"]], [second.id])

    def test_agent_task_claim_hides_claimed_events_until_expired(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        second = events.append("call-1", "agent_response_requested", {"text": "second"})

        claim_response = client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id, second.id], "owner": "worker-1", "ttl_seconds": 30},
        )
        tasks_response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(claim_response.status_code, 200)
        self.assertEqual(claim_response.json()["claimed_event_ids"], [first.id, second.id])
        self.assertEqual(tasks_response.status_code, 200)
        self.assertEqual(tasks_response.json()["pending"], [])
        claim_events = [
            event
            for event in events.list_events(call_id="call-1")
            if event.type == "agent_task_claimed"
        ]
        self.assertEqual([event.data["task_event_id"] for event in claim_events], [first.id, second.id])
        self.assertEqual([event.data["owner"] for event in claim_events], ["worker-1", "worker-1"])
        metric_events = [
            event
            for event in events.list_events(call_id="call-1")
            if event.type == "metrics" and event.data.get("name") == "agent_task_pickup_latency_seconds"
        ]
        self.assertEqual([event.data["task_event_id"] for event in metric_events], [first.id, second.id])

    def test_agent_tasks_include_confirmed_speculative_task_context(self) -> None:
        coordinator = SubagentCoordinator()
        coordinator.register(CompletedSubagentProvider())
        client, events, _tracker = self.build_client_with_subagents(coordinator)
        partial = events.append("call-1", "user_transcript_partial", {"turn_id": 1, "text": "please check website status"})
        final = events.append("call-1", "agent_response_requested", {"turn_id": 1, "text": "please check website status"})
        task = coordinator.request_speculative(
            SubagentTaskRequest(
                workspace_id="workspace-1",
                voicebot_id="voicebot-1",
                session_id="call-1",
                request_event_id=partial.id,
                provider="flowhunt_flow",
                input_text="please check website status",
                metadata={"partial_event_id": partial.id, "partial_text": "please check website status"},
            ),
            speculative_key="call-1:turn:1",
        )
        coordinator.confirm_speculative(
            task.task_id,
            "workspace-1",
            final_request_event_id=final.id,
            final_input_text="please check website status",
        )

        response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(response.status_code, 200)
        task_data = response.json()["pending"][0]["data"]
        self.assertEqual(task_data["confirmed_speculative_task_id"], task.task_id)
        self.assertTrue(task_data["speculative_reused"])
        self.assertEqual(task_data["confirmed_speculative_task"]["result"]["summary"], "The speculative result is ready.")

    def test_redis_agent_task_claim_is_shared_across_worker_clients(self) -> None:
        events = EventStore(max_context_events=50)
        redis = FakeRedis()
        tracker_1 = RedisAgentTaskTracker("redis://test", client=redis)
        tracker_2 = RedisAgentTaskTracker("redis://test", client=redis)
        first_app = create_app(
            events,
            FakeCallRegistry(["call-1"]),
            tracker_1,
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        second_app = create_app(
            events,
            FakeCallRegistry(["call-1"]),
            tracker_2,
            WebSocketHub(),
            TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
            None,
        )
        first_client = TestClient(first_app)
        second_client = TestClient(second_app)
        task = events.append("call-1", "agent_response_requested", {"text": "first"})

        first_claim = first_client.post(
            "/agent/tasks/claim",
            json={"event_ids": [task.id], "owner": "worker-1", "ttl_seconds": 30},
        )
        second_claim = second_client.post(
            "/agent/tasks/claim",
            json={"event_ids": [task.id], "owner": "worker-2", "ttl_seconds": 30},
        )
        second_pending = second_client.get("/agent/tasks?call_id=call-1")
        second_status = second_client.get("/agent/tasks/status")

        self.assertEqual(first_claim.status_code, 200)
        self.assertEqual(second_claim.status_code, 200)
        self.assertEqual(first_claim.json()["claimed_event_ids"], [task.id])
        self.assertEqual(second_claim.json()["claimed_event_ids"], [])
        self.assertEqual(second_pending.json()["pending"], [])
        self.assertEqual(second_status.json()["claims"][str(task.id)]["owner"], "worker-1")

    def test_agent_tasks_ignore_claim_events_when_listing_pending_tasks(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id], "owner": "worker-1", "ttl_seconds": 0.1},
        )

        time.sleep(0.12)
        response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([event["id"] for event in response.json()["pending"]], [first.id])

    def test_agent_task_claim_skips_responded_events(self) -> None:
        client, events, tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        tracker.mark_responded(first.id)

        response = client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id], "owner": "worker-1", "ttl_seconds": 30},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["claimed_event_ids"], [])

    def test_agent_task_claim_skips_missing_non_task_and_inactive_events(self) -> None:
        client, events, _tracker = self.build_client()
        non_task = events.append("call-1", "user_transcript", {"text": "not a task"})
        inactive_task = events.append("inactive", "agent_response_requested", {"text": "inactive"})

        response = client.post(
            "/agent/tasks/claim",
            json={"event_ids": [999999, non_task.id, inactive_task.id], "owner": "worker-1", "ttl_seconds": 30},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["claimed_event_ids"], [])
        self.assertEqual(events.list_events(call_id="call-1")[-1].type, "user_transcript")

    def test_agent_task_claim_expires(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})

        claim_response = client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id], "owner": "worker-1", "ttl_seconds": 0.1},
        )
        self.assertEqual(claim_response.json()["claimed_event_ids"], [first.id])

        time.sleep(0.12)
        tasks_response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual([event["id"] for event in tasks_response.json()["pending"]], [first.id])

    def test_agent_task_release_makes_claimed_events_pending_again(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})

        claim_response = client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id], "owner": "worker-1", "ttl_seconds": 30},
        )
        release_response = client.post("/agent/tasks/release", json={"event_ids": [first.id], "owner": "worker-1"})
        tasks_response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(claim_response.json()["claimed_event_ids"], [first.id])
        self.assertEqual(release_response.status_code, 200)
        self.assertEqual(release_response.json()["released_event_ids"], [first.id])
        self.assertEqual([event["id"] for event in tasks_response.json()["pending"]], [first.id])
        release_events = [
            event
            for event in events.list_events(call_id="call-1")
            if event.type == "agent_task_released"
        ]
        self.assertEqual([event.data["task_event_id"] for event in release_events], [first.id])
        self.assertEqual([event.data["owner"] for event in release_events], ["worker-1"])

    def test_agent_task_release_skips_claims_owned_by_another_worker(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id], "owner": "worker-1", "ttl_seconds": 30},
        )

        release_response = client.post("/agent/tasks/release", json={"event_ids": [first.id], "owner": "worker-2"})
        tasks_response = client.get("/agent/tasks?call_id=call-1")

        self.assertEqual(release_response.status_code, 200)
        self.assertEqual(release_response.json()["released_event_ids"], [])
        self.assertEqual(tasks_response.json()["pending"], [])

    def test_agent_task_status_reports_claims_and_responded_events(self) -> None:
        client, events, tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        second = events.append("call-1", "agent_response_requested", {"text": "second"})
        tracker.mark_responded(first.id)
        client.post(
            "/agent/tasks/claim",
            json={"event_ids": [second.id], "owner": "worker-1", "ttl_seconds": 30},
        )

        response = client.get("/agent/tasks/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["responded_event_ids"], [first.id])
        self.assertEqual(payload["claims"][str(second.id)]["owner"], "worker-1")
        self.assertGreater(payload["claims"][str(second.id)]["expires_in_seconds"], 0)

    def test_agent_task_status_can_filter_claims_by_owner(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        second = events.append("call-2", "agent_response_requested", {"text": "second"})
        client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id], "owner": "worker-1", "ttl_seconds": 30},
        )
        client.post(
            "/agent/tasks/claim",
            json={"event_ids": [second.id], "owner": "worker-2", "ttl_seconds": 30},
        )

        response = client.get("/agent/tasks/status?owner=worker-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.json()["claims"]), [str(first.id)])

    def test_get_agent_task_status_tool_reports_claims(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id], "owner": "worker-1", "ttl_seconds": 30},
        )

        response = client.post("/agent/tools/get_agent_task_status", json={"arguments": {"owner": "worker-1"}})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.json()["claims"]), [str(first.id)])

    def test_agent_task_summary_classifies_tasks(self) -> None:
        client, events, tracker = self.build_client()
        responded = events.append("call-1", "agent_response_requested", {"text": "responded"})
        claimed = events.append("call-1", "agent_response_requested", {"text": "claimed"})
        pending = events.append("call-2", "agent_response_requested", {"text": "pending"})
        inactive = events.append("inactive", "agent_response_requested", {"text": "inactive"})
        tracker.mark_responded(responded.id)
        client.post(
            "/agent/tasks/claim",
            json={"event_ids": [claimed.id], "owner": "worker-1", "ttl_seconds": 30},
        )

        response = client.get("/agent/tasks/summary")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        states = {task["event"]["id"]: task["state"] for task in payload["tasks"]}
        self.assertEqual(states[responded.id], "responded")
        self.assertEqual(states[claimed.id], "claimed")
        self.assertEqual(states[pending.id], "pending")
        self.assertEqual(states[inactive.id], "inactive")
        self.assertEqual(payload["counts"], {"responded": 1, "claimed": 1, "pending": 1, "inactive": 1})

    def test_agent_task_summary_filters_by_call_owner_and_limit(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        second = events.append("call-1", "agent_response_requested", {"text": "second"})
        events.append("call-2", "agent_response_requested", {"text": "other"})
        client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id], "owner": "worker-1", "ttl_seconds": 30},
        )
        client.post(
            "/agent/tasks/claim",
            json={"event_ids": [second.id], "owner": "worker-2", "ttl_seconds": 30},
        )

        response = client.get("/agent/tasks/summary?call_id=call-1&owner=worker-1&limit=1")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([task["event"]["id"] for task in payload["tasks"]], [first.id])
        self.assertEqual(payload["counts"], {"claimed": 1})

    def test_agent_task_summary_applies_after_cursor(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        second = events.append("call-1", "agent_response_requested", {"text": "second"})

        response = client.get(f"/agent/tasks/summary?after={first.id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([task["event"]["id"] for task in response.json()["tasks"]], [second.id])

    def test_get_agent_task_summary_tool_reports_classified_tasks(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})

        response = client.post(
            "/agent/tools/get_agent_task_summary",
            json={"arguments": {"call_id": "call-1", "limit": 1}},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([task["event"]["id"] for task in response.json()["tasks"]], [first.id])

    def test_get_agent_task_summary_tool_applies_after_cursor(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        second = events.append("call-1", "agent_response_requested", {"text": "second"})

        response = client.post(
            "/agent/tools/get_agent_task_summary",
            json={"arguments": {"after": first.id}},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([task["event"]["id"] for task in response.json()["tasks"]], [second.id])

    def test_agent_task_renew_extends_matching_claims(self) -> None:
        client, events, _tracker = self.build_client()
        first = events.append("call-1", "agent_response_requested", {"text": "first"})
        client.post(
            "/agent/tasks/claim",
            json={"event_ids": [first.id], "owner": "worker-1", "ttl_seconds": 0.1},
        )

        wrong_owner = client.post(
            "/agent/tasks/renew",
            json={"event_ids": [first.id], "owner": "worker-2", "ttl_seconds": 30},
        )
        renewed = client.post(
            "/agent/tasks/renew",
            json={"event_ids": [first.id], "owner": "worker-1", "ttl_seconds": 30},
        )

        self.assertEqual(wrong_owner.status_code, 200)
        self.assertEqual(wrong_owner.json()["renewed_event_ids"], [])
        self.assertEqual(renewed.status_code, 200)
        self.assertEqual(renewed.json()["renewed_event_ids"], [first.id])
        renew_events = [
            event
            for event in events.list_events(call_id="call-1")
            if event.type == "agent_task_renewed"
        ]
        self.assertEqual([event.data["task_event_id"] for event in renew_events], [first.id])
        self.assertEqual([event.data["owner"] for event in renew_events], ["worker-1"])


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, float | None]] = {}

    def ping(self) -> bool:
        return True

    def get(self, key: str) -> str | None:
        self._expire()
        item = self.values.get(key)
        return item[0] if item is not None else None

    def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool:
        self._expire()
        if nx and key in self.values:
            return False
        if px is not None:
            expires_at = time.monotonic() + (px / 1000)
        elif ex is not None:
            expires_at = time.monotonic() + ex
        else:
            expires_at = None
        self.values[key] = (value, expires_at)
        return True

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.values:
                removed += 1
            self.values.pop(key, None)
        return removed

    def keys(self, pattern: str) -> list[str]:
        self._expire()
        prefix = pattern.rstrip("*")
        return [key for key in self.values if key.startswith(prefix)]

    def ttl(self, key: str) -> int:
        self._expire()
        item = self.values.get(key)
        if item is None:
            return -2
        expires_at = item[1]
        if expires_at is None:
            return -1
        return max(0, int(expires_at - time.monotonic()))

    def _expire(self) -> None:
        now = time.monotonic()
        expired = [key for key, (_value, expires_at) in self.values.items() if expires_at is not None and expires_at <= now]
        for key in expired:
            self.values.pop(key, None)


if __name__ == "__main__":
    unittest.main()
