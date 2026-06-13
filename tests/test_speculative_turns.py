from __future__ import annotations

import unittest

from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.speculative_turns import SpeculativeTurnCoordinator
from voicebot.subagents import (
    SubagentCoordinator,
    SubagentProviderDescriptor,
    SubagentTask,
    SubagentTaskRequest,
    SubagentTaskResult,
    SubagentTaskStore,
)


class FakeStreamingRagProvider:
    kind = "internal_worker"

    def __init__(self, *, submit_status: str = "running") -> None:
        self.submit_status = submit_status
        self.requests: list[SubagentTaskRequest] = []
        self.cancelled: list[str] = []

    def submit(self, request: SubagentTaskRequest) -> SubagentTask:
        self.requests.append(request)
        task, _created = SubagentTaskStore().get_or_create_requested(request)
        if self.submit_status == "completed":
            return task.with_status(
                "completed",
                result=SubagentTaskResult(
                    summary="The cached result is ready.",
                    context={"result_fingerprints": ["result-1"]},
                ),
            )
        return task.with_status("running", external_task_id=f"external-{len(self.requests)}")

    def poll(self, task: SubagentTask) -> SubagentTask:
        return task

    def cancel(self, task: SubagentTask) -> SubagentTask:
        self.cancelled.append(task.task_id)
        return task.with_status("cancelled")


class SpeculativeTurnCoordinatorTests(unittest.TestCase):
    def test_model_triggered_mode_supersedes_previous_partial_query(self) -> None:
        events, coordinator, provider = self._coordinator()
        first = events.append("call-1", "user_transcript_partial", {"turn_id": 7, "text": "please check website status"})
        second = events.append("call-1", "user_transcript_partial", {"turn_id": 7, "text": "please check website pricing"})

        first_task = coordinator.observe_partial(first)
        second_task = coordinator.observe_partial(second)

        self.assertIsNotNone(first_task)
        self.assertIsNotNone(second_task)
        self.assertEqual(len(provider.requests), 2)
        stored = {task.task_id: task for task in coordinator.subagent_coordinator.store.list(workspace_id="workspace-1")}
        self.assertEqual(stored[first_task.task_id].metadata["speculative_status"], "superseded")
        self.assertEqual(stored[first_task.task_id].metadata["speculative_cancel_reason"], "superseded_by_new_partial_query")
        self.assertEqual(stored[second_task.task_id].metadata["speculative_status"], "started")
        self.assertEqual(provider.cancelled, [first_task.task_id])

    def test_legacy_speculative_mode_keeps_one_candidate_without_replacement(self) -> None:
        events, coordinator, provider = self._coordinator(streaming_rag_enabled=False, speculative_work_enabled=True)
        first = events.append("call-1", "user_transcript_partial", {"turn_id": 7, "text": "please check website status"})
        second = events.append("call-1", "user_transcript_partial", {"turn_id": 7, "text": "please check website pricing"})

        first_task = coordinator.observe_partial(first)
        second_task = coordinator.observe_partial(second)

        self.assertIsNotNone(first_task)
        self.assertIsNone(second_task)
        self.assertEqual(len(provider.requests), 1)
        stored = coordinator.subagent_coordinator.store.get(first_task.task_id, "workspace-1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.metadata["speculative_status"], "started")
        self.assertNotIn("streaming_rag_candidate", stored.metadata)
        self.assertEqual(provider.cancelled, [])

    def test_fixed_interval_mode_caps_parallel_candidates_and_dedupes_query_hashes(self) -> None:
        events, coordinator, provider = self._coordinator(
            streaming_rag_trigger_mode="fixed_interval",
            streaming_rag_max_parallel_per_turn=2,
        )
        partials = [
            events.append("call-1", "user_transcript_partial", {"turn_id": 3, "text": "please check website status"}),
            events.append("call-1", "user_transcript_partial", {"turn_id": 3, "text": "please check website pricing"}),
            events.append("call-1", "user_transcript_partial", {"turn_id": 3, "text": "please check website account"}),
            events.append("call-1", "user_transcript_partial", {"turn_id": 3, "text": "please check website pricing"}),
        ]

        results = [coordinator.observe_partial(event) for event in partials]

        self.assertIsNotNone(results[0])
        self.assertIsNotNone(results[1])
        self.assertIsNone(results[2])
        self.assertIsNone(results[3])
        self.assertEqual(len(provider.requests), 2)
        query_hashes = {request.metadata["query_hash"] for request in provider.requests}
        self.assertEqual(len(query_hashes), 2)

    def test_matching_final_transcript_reuses_completed_candidate(self) -> None:
        events, coordinator, provider = self._coordinator(submit_status="completed")
        partial = events.append("call-1", "user_transcript_partial", {"turn_id": 4, "text": "please check website status"})
        final = events.append(
            "call-1",
            "agent_response_requested",
            {"turn_id": 4, "text": "please check the website status for me"},
        )

        task = coordinator.observe_partial(partial)
        confirmed = coordinator.reconcile_final_request(
            turn_id=4,
            final_text=final.data["text"],
            final_request_event_id=final.id,
        )

        self.assertIsNotNone(task)
        self.assertIsNotNone(confirmed)
        self.assertEqual(len(provider.requests), 1)
        stored = coordinator.subagent_coordinator.store.get(task.task_id, "workspace-1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.metadata["speculative_status"], "confirmed")
        self.assertEqual(stored.metadata["streaming_rag_reflector_decision"], "reuse")
        self.assertEqual(stored.metadata["streaming_rag_final_request_event_id"], final.id)
        metrics = [
            event.data
            for event in events.list_events(call_id="call-1")
            if event.type == "metrics" and event.data.get("name") == "streaming_rag_reflector_decision"
        ]
        self.assertEqual(metrics[-1]["decision"], "reuse")
        metric_names = {
            event.data["name"]
            for event in events.list_events(call_id="call-1")
            if event.type == "metrics"
        }
        self.assertIn("partial_stt_to_speculative_start_seconds", metric_names)
        self.assertIn("speculative_task_completed_before_final_transcript", metric_names)
        self.assertIn("speculative_result_reuse_latency_seconds", metric_names)

    def test_changed_external_final_supersedes_non_cancellable_candidate(self) -> None:
        events, coordinator, provider = self._coordinator(provider_supports_cancel=False)
        partial = events.append("call-1", "user_transcript_partial", {"turn_id": 5, "text": "please check website status"})
        final = events.append(
            "call-1",
            "agent_response_requested",
            {"turn_id": 5, "text": "please verify latest account pricing"},
        )

        task = coordinator.observe_partial(partial)
        result = coordinator.reconcile_final_request(
            turn_id=5,
            final_text=final.data["text"],
            final_request_event_id=final.id,
        )

        self.assertIsNotNone(task)
        self.assertIsNone(result)
        self.assertEqual(provider.cancelled, [])
        stored = coordinator.subagent_coordinator.store.get(task.task_id, "workspace-1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.metadata["speculative_status"], "superseded")
        self.assertEqual(stored.metadata["speculative_cancel_reason"], "final_transcript_changed")
        metrics = [
            event.data
            for event in events.list_events(call_id="call-1")
            if event.type == "metrics" and event.data.get("name") == "streaming_rag_reflector_decision"
        ]
        self.assertEqual(metrics[-1]["decision"], "supersede")
        self.assertTrue(metrics[-1]["cancelled_candidates"])

    def test_final_without_external_work_cancels_candidate(self) -> None:
        events, coordinator, _provider = self._coordinator()
        partial = events.append("call-1", "user_transcript_partial", {"turn_id": 9, "text": "please check website status"})
        final = events.append("call-1", "agent_response_requested", {"turn_id": 9, "text": "thanks goodbye"})

        task = coordinator.observe_partial(partial)
        result = coordinator.reconcile_final_request(
            turn_id=9,
            final_text=final.data["text"],
            final_request_event_id=final.id,
        )

        self.assertIsNotNone(task)
        self.assertIsNone(result)
        stored = coordinator.subagent_coordinator.store.get(task.task_id, "workspace-1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.metadata["speculative_status"], "cancelled")
        metrics = [
            event.data
            for event in events.list_events(call_id="call-1")
            if event.type == "metrics" and event.data.get("name") == "streaming_rag_reflector_decision"
        ]
        self.assertEqual(metrics[-1]["decision"], "cancel")

    def _coordinator(self, **settings_overrides):
        events = EventStore(max_context_events=100)
        provider = FakeStreamingRagProvider(submit_status=settings_overrides.pop("submit_status", "running"))
        subagents = SubagentCoordinator(events=events)
        provider_supports_cancel = settings_overrides.pop("provider_supports_cancel", True)
        subagents.register(
            provider,
            SubagentProviderDescriptor(
                kind=provider.kind,
                label="Fake Streaming RAG provider",
                supports_cancel=provider_supports_cancel,
            ),
        )
        defaults = {
            "streaming_rag_enabled": True,
            "speculative_min_chars": 8,
            "speculative_min_tokens": 2,
            "speculative_external_intent_required": True,
        }
        settings = Settings(**{**defaults, **settings_overrides})
        coordinator = SpeculativeTurnCoordinator(
            settings=settings,
            events=events,
            subagent_coordinator=subagents,
            scope_resolver=lambda call_id: {
                "workspace_id": "workspace-1",
                "voicebot_id": "voicebot-1",
                "session_id": "session-1",
            },
        )
        return events, coordinator, provider
