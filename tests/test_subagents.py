from __future__ import annotations

from dataclasses import dataclass, replace as dataclasses_replace
import unittest

from voicebot.subagents import (
    FlowHuntSubagentProvider,
    SubagentProviderDescriptor,
    SubagentCoordinator,
    SubagentTask,
    SubagentTaskRequest,
    SubagentTaskResult,
    SubagentTaskStore,
)
from voicebot.events import EventStore


class FakeProvider:
    kind = "internal_worker"

    def __init__(self) -> None:
        self.submitted = 0

    def submit(self, request: SubagentTaskRequest) -> SubagentTask:
        self.submitted += 1
        task, _created = SubagentTaskStore().get_or_create_requested(request)
        return task.with_status("running", external_task_id="external-1", progress_message="Working on it.")

    def poll(self, task: SubagentTask) -> SubagentTask:
        return task.with_status(
            "completed",
            result=SubagentTaskResult(
                summary="The colleague found the answer.",
                content="There are 42 pages.",
                context={"confidence": "high"},
                provider_payload={"raw": "not for speech"},
            ),
        )

    def cancel(self, task: SubagentTask) -> SubagentTask:
        return task.with_status("cancelled")


@dataclass
class FakeFlowHuntResult:
    ok: bool
    message: str
    data: dict


class FakeFlowHuntClient:
    def __init__(self) -> None:
        self.invoked = []
        self.polled = []

    def invoke_flow_and_wait(self, flow_id: str, message: str, wait_seconds: float, poll_interval_seconds: float):
        self.invoked.append((flow_id, message, wait_seconds, poll_interval_seconds))
        return FakeFlowHuntResult(True, "FlowHunt flow was invoked.", {"task_id": "task-1", "pending": True})

    def get_flow_task(self, flow_id: str, task_id: str):
        self.polled.append((flow_id, task_id))
        return FakeFlowHuntResult(True, "The answer is 42.", {"status": "completed", "result": "The answer is 42."})


class SubagentTests(unittest.TestCase):
    def request(self) -> SubagentTaskRequest:
        return SubagentTaskRequest(
            workspace_id="workspace-1",
            session_id="call-1",
            request_event_id=10,
            provider="internal_worker",
            input_text="How many pages?",
            voicebot_id="voicebot-1",
        )

    def test_store_deduplicates_by_workspace_session_and_request_event(self) -> None:
        store = SubagentTaskStore()
        first, first_created = store.get_or_create_requested(self.request())
        second, second_created = store.get_or_create_requested(self.request())

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.task_id, second.task_id)

    def test_coordinator_submits_polls_and_returns_clean_result_context(self) -> None:
        provider = FakeProvider()
        coordinator = SubagentCoordinator()
        coordinator.register(provider)

        task = coordinator.request(self.request())
        polled = coordinator.poll(task.task_id, "workspace-1")

        self.assertEqual(provider.submitted, 1)
        self.assertEqual(polled.status, "completed")
        self.assertEqual(
            polled.clean_result_context(),
            {
                "task_id": polled.task_id,
                "status": "completed",
                "provider": "internal_worker",
                "summary": "The colleague found the answer.",
                "content": "There are 42 pages.",
                "context": {"confidence": "high"},
            },
        )

    def test_coordinator_blocks_cross_workspace_reads(self) -> None:
        coordinator = SubagentCoordinator()
        coordinator.register(FakeProvider())
        task = coordinator.request(self.request())

        with self.assertRaisesRegex(KeyError, "unknown subagent task"):
            coordinator.poll(task.task_id, "workspace-2")

    def test_store_rejects_task_identity_moves(self) -> None:
        store = SubagentTaskStore()
        task, _created = store.get_or_create_requested(self.request())
        invalid_tasks = [
            (dataclasses_replace(task, session_id="call-2"), "sessions"),
            (dataclasses_replace(task, voicebot_id="voicebot-2"), "voicebots"),
            (dataclasses_replace(task, provider="flowhunt_flow"), "providers"),
            (dataclasses_replace(task, request_event_id=11), "request events"),
        ]

        for invalid, message in invalid_tasks:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    store.update(invalid)

    def test_duplicate_request_does_not_submit_provider_twice(self) -> None:
        provider = FakeProvider()
        coordinator = SubagentCoordinator()
        coordinator.register(provider)

        first = coordinator.request(self.request())
        second = coordinator.request(self.request())

        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(provider.submitted, 1)

    def test_coordinator_emits_workspace_scoped_lifecycle_events(self) -> None:
        provider = FakeProvider()
        events = EventStore(max_context_events=20)
        coordinator = SubagentCoordinator(events=events)
        coordinator.register(provider)

        task = coordinator.request(self.request())
        coordinator.request(self.request())
        coordinator.poll(task.task_id, "workspace-1")

        event_types = [event.type for event in events.list_events(call_id="call-1")]
        self.assertEqual(
            event_types,
            [
                "subagent_task_requested",
                "subagent_task_updated",
                "subagent_task_deduplicated",
                "subagent_task_updated",
            ],
        )
        first = events.list_events(call_id="call-1")[0]
        self.assertEqual(first.data["workspace_id"], "workspace-1")
        self.assertEqual(first.data["voicebot_id"], "voicebot-1")
        self.assertEqual(first.data["session_id"], "call-1")
        self.assertEqual(first.data["task_id"], task.task_id)
        self.assertNotIn("provider_payload", str(first.data))

    def test_task_event_context_exposes_clean_result_context_only(self) -> None:
        provider = FakeProvider()
        coordinator = SubagentCoordinator()
        coordinator.register(provider)

        task = coordinator.request(self.request())
        completed = coordinator.poll(task.task_id, "workspace-1")

        context = completed.event_context()
        self.assertEqual(context["result"]["summary"], "The colleague found the answer.")
        self.assertNotIn("provider_payload", context["result"])

    def test_speculative_task_can_be_confirmed_or_cancelled(self) -> None:
        provider = FakeProvider()
        events = EventStore(max_context_events=50)
        coordinator = SubagentCoordinator(events=events)
        coordinator.register(provider)

        task = coordinator.request_speculative(self.request(), speculative_key="turn-1")
        confirmed = coordinator.confirm_speculative(
            task.task_id,
            "workspace-1",
            final_request_event_id=11,
            final_input_text="How many pages are on the site?",
        )
        cancelled = coordinator.cancel_speculative(task.task_id, "workspace-1", reason="superseded_by_final_text")

        self.assertTrue(task.metadata["speculative"])
        self.assertEqual(confirmed.metadata["speculative_status"], "confirmed")
        self.assertEqual(confirmed.metadata["final_request_event_id"], 11)
        self.assertEqual(cancelled.metadata["speculative_status"], "cancelled")
        event_types = [event.type for event in events.list_events(call_id="call-1")]
        self.assertIn("subagent_task_speculative_started", event_types)
        self.assertIn("subagent_task_speculative_confirmed", event_types)
        self.assertIn("subagent_task_speculative_cancelled", event_types)

    def test_speculative_task_can_be_superseded_by_replacement_request(self) -> None:
        provider = FakeProvider()
        events = EventStore(max_context_events=50)
        coordinator = SubagentCoordinator(events=events)
        coordinator.register(provider)

        original = coordinator.request_speculative(self.request(), speculative_key="turn-1")
        replacement = dataclasses_replace(
            self.request(),
            request_event_id=12,
            input_text="Check the final corrected request.",
            dedupe_key="final-12",
        )
        superseded, new_task = coordinator.supersede_speculative(original.task_id, "workspace-1", replacement)

        self.assertEqual(superseded.metadata["speculative_status"], "superseded")
        self.assertNotEqual(superseded.task_id, new_task.task_id)
        self.assertEqual(new_task.input_text, "Check the final corrected request.")
        event_types = [event.type for event in events.list_events(call_id="call-1")]
        self.assertIn("subagent_task_speculative_superseded", event_types)

    def test_flowhunt_provider_uses_flow_invoke_task_protocol(self) -> None:
        client = FakeFlowHuntClient()
        provider = FlowHuntSubagentProvider("flowhunt_flow", client, "flow-1")
        request = SubagentTaskRequest(
            workspace_id="workspace-1",
            session_id="call-1",
            request_event_id=10,
            provider="flowhunt_flow",
            input_text="Count pages",
        )

        submitted = provider.submit(request)
        completed = provider.poll(submitted)

        self.assertEqual(client.invoked, [("flow-1", "Count pages", 0, 3)])
        self.assertEqual(client.polled, [("flow-1", "task-1")])
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.result.summary, "The answer is 42.")

    def test_flowhunt_provider_ignores_request_metadata_target_override(self) -> None:
        client = FakeFlowHuntClient()
        provider = FlowHuntSubagentProvider("flowhunt_flow", client, "configured-flow")
        request = SubagentTaskRequest(
            workspace_id="workspace-1",
            session_id="call-1",
            request_event_id=10,
            provider="flowhunt_flow",
            input_text="Count pages",
            metadata={"flow_id": "agent-supplied-flow"},
        )

        submitted = provider.submit(request)

        self.assertEqual(client.invoked, [("configured-flow", "Count pages", 0, 3)])
        self.assertEqual(submitted.provider_references["target_id"], "configured-flow")

    def test_flowhunt_provider_uses_prompt_hook_progress_from_metadata(self) -> None:
        client = FakeFlowHuntClient()
        provider = FlowHuntSubagentProvider("flowhunt_flow", client, "flow-1")
        request = SubagentTaskRequest(
            workspace_id="workspace-1",
            session_id="call-1",
            request_event_id=10,
            provider="flowhunt_flow",
            input_text="Count pages",
            metadata={"after_call_text": "The specialist is checking it now."},
        )

        submitted = provider.submit(request)

        self.assertEqual(submitted.status, "running")
        self.assertEqual(submitted.progress_messages[-1], "The specialist is checking it now.")

    def test_coordinator_exposes_registered_subagent_provider_catalog(self) -> None:
        coordinator = SubagentCoordinator()
        coordinator.register(FakeProvider())

        catalog = coordinator.provider_catalog()

        self.assertIn("internal_worker", catalog["providers"])
        self.assertTrue(catalog["providers"]["internal_worker"]["registered"])
        self.assertFalse(catalog["providers"]["flowhunt_flow"]["registered"])
        self.assertEqual(catalog["providers"]["internal_worker"]["result_context"], "clean")

    def test_coordinator_accepts_custom_subagent_provider_descriptor(self) -> None:
        coordinator = SubagentCoordinator()
        coordinator.register(
            FakeProvider(),
            SubagentProviderDescriptor(
                kind="internal_worker",
                label="Custom internal worker",
                required_metadata=("skill",),
            ),
        )

        provider = coordinator.provider_catalog()["providers"]["internal_worker"]

        self.assertEqual(provider["label"], "Custom internal worker")
        self.assertEqual(provider["required_metadata"], ["skill"])

    def test_coordinator_rejects_invalid_subagent_provider_descriptor(self) -> None:
        coordinator = SubagentCoordinator()

        with self.assertRaisesRegex(ValueError, "invalid subagent provider descriptor"):
            coordinator.register(
                FakeProvider(),
                SubagentProviderDescriptor(
                    kind="internal_worker",
                    label="",
                    required_metadata=("",),
                ),
            )

    def test_default_subagent_provider_descriptors_are_valid(self) -> None:
        coordinator = SubagentCoordinator()

        issues = [
            (kind, issue)
            for kind, descriptor in coordinator.provider_descriptors.items()
            for issue in descriptor.validation_issues()
        ]

        self.assertEqual(issues, [])

    def test_coordinator_validates_required_provider_metadata_before_submit(self) -> None:
        provider = FakeProvider()
        coordinator = SubagentCoordinator()
        coordinator.register(
            provider,
            SubagentProviderDescriptor(
                kind="internal_worker",
                label="Custom internal worker",
                required_metadata=("skill",),
            ),
        )

        with self.assertRaisesRegex(ValueError, "requires metadata: skill"):
            coordinator.request(self.request())

        request = SubagentTaskRequest(
            workspace_id="workspace-1",
            session_id="call-1",
            request_event_id=11,
            provider="internal_worker",
            input_text="Use skill",
            metadata={"skill": "research"},
        )
        coordinator.request(request)

        self.assertEqual(provider.submitted, 1)


if __name__ == "__main__":
    unittest.main()
