from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import tempfile
import unittest

from voicebot.subagents import (
    JsonSubagentTaskStore,
    SubagentCoordinator,
    SubagentProviderDescriptor,
    SubagentTask,
    SubagentTaskRequest,
    SubagentTaskResult,
    SubagentTaskStore,
)
from voicebot.task_lifecycle import PollingPolicy, SubagentTaskLifecycleRunner


class SequencedProvider:
    kind = "internal_worker"

    def __init__(self, outcomes: list[str] | None = None, raise_on_poll: bool = False) -> None:
        self.outcomes = outcomes or ["running", "completed"]
        self.raise_on_poll = raise_on_poll
        self.polls = 0
        self.cancels = 0

    def submit(self, request: SubagentTaskRequest) -> SubagentTask:
        task, _created = SubagentTaskStore().get_or_create_requested(request)
        return task.with_status("running", external_task_id="external-1")

    def poll(self, task: SubagentTask) -> SubagentTask:
        self.polls += 1
        if self.raise_on_poll:
            raise RuntimeError("provider unavailable")
        outcome = self.outcomes[min(self.polls - 1, len(self.outcomes) - 1)]
        if outcome == "completed":
            return task.with_status("completed", result=SubagentTaskResult(summary="done"))
        return task.with_status("running", progress_message="still working")

    def cancel(self, task: SubagentTask) -> SubagentTask:
        self.cancels += 1
        return task.with_status("cancelled")


class TaskLifecycleTests(unittest.TestCase):
    def build_coordinator(self, provider: SequencedProvider | None = None) -> SubagentCoordinator:
        coordinator = SubagentCoordinator()
        coordinator.register(provider or SequencedProvider())
        return coordinator

    def request(self) -> SubagentTaskRequest:
        return SubagentTaskRequest(
            workspace_id="workspace-1",
            session_id="call-1",
            request_event_id=10,
            provider="internal_worker",
            input_text="check this",
        )

    def test_json_store_persists_external_task_ids_and_dedupe_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/tasks.json"
            store = JsonSubagentTaskStore(path)
            task, created = store.get_or_create_requested(self.request())
            store.update(task.with_status("running", external_task_id="external-1"))

            reloaded = JsonSubagentTaskStore(path)
            duplicate, duplicate_created = reloaded.get_or_create_requested(self.request())

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.task_id, task.task_id)
        self.assertEqual(duplicate.external_task_id, "external-1")

    def test_json_store_reports_load_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/tasks.json"
            first = JsonSubagentTaskStore(path)
            task, _created = first.get_or_create_requested(self.request())
            with open(path, encoding="utf-8") as handle:
                payload = json.loads(handle.read())
            payload["tasks"].append({"task_id": "bad"})
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)

            reloaded = JsonSubagentTaskStore(path)

        self.assertEqual(reloaded.load_diagnostics["loaded_tasks"], 1)
        self.assertEqual(reloaded.load_diagnostics["skipped_invalid_tasks"], 1)
        self.assertEqual(reloaded.get(task.task_id).task_id, task.task_id)

    def test_json_store_reports_malformed_json_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/tasks.json"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{bad json}")

            reloaded = JsonSubagentTaskStore(path)

        self.assertEqual(reloaded.load_diagnostics["skipped_malformed_json"], 1)
        self.assertEqual(reloaded.list(), [])

    def test_lifecycle_runner_polls_due_task_with_backoff_and_emits_completion_once(self) -> None:
        provider = SequencedProvider(["running", "completed"])
        coordinator = self.build_coordinator(provider)
        events = []
        runner = SubagentTaskLifecycleRunner(
            coordinator,
            policy=PollingPolicy(initial_interval_seconds=3, max_interval_seconds=10, timeout_seconds=60),
            event_sink=lambda event_type, task: events.append((event_type, task.task_id)),
        )
        now = datetime(2026, 5, 28, tzinfo=UTC)
        task = runner.schedule(coordinator.request(self.request()), now)

        first = runner.tick(now + timedelta(seconds=3))[0]
        second = runner.tick(now + timedelta(seconds=9))[0]
        runner.tick(now + timedelta(seconds=12))

        self.assertEqual(first.status, "running")
        self.assertEqual(first.attempts, 1)
        self.assertEqual(second.status, "completed")
        self.assertEqual(events, [("subagent_task_completed", second.task_id)])
        self.assertIsNotNone(coordinator.store.get(second.task_id).terminal_event_emitted_at)

    def test_lifecycle_runner_can_schedule_pending_tasks_after_restart(self) -> None:
        coordinator = self.build_coordinator(SequencedProvider(["running"]))
        runner = SubagentTaskLifecycleRunner(
            coordinator,
            policy=PollingPolicy(initial_interval_seconds=3, timeout_seconds=60),
        )
        now = datetime(2026, 5, 28, tzinfo=UTC)
        first = coordinator.request(self.request())
        second = coordinator.request(
            SubagentTaskRequest(
                workspace_id="workspace-2",
                session_id="call-2",
                request_event_id=11,
                provider="internal_worker",
                input_text="check another",
            )
        )

        scheduled = runner.schedule_pending(workspace_id="workspace-1", now=now)

        self.assertEqual([task.task_id for task in scheduled], [first.task_id])
        self.assertIsNotNone(coordinator.store.get(first.task_id).next_poll_at)
        self.assertIsNone(coordinator.store.get(second.task_id).next_poll_at)

    def test_lifecycle_runner_retries_provider_errors_until_max_attempts(self) -> None:
        coordinator = self.build_coordinator(SequencedProvider(raise_on_poll=True))
        events = []
        runner = SubagentTaskLifecycleRunner(
            coordinator,
            policy=PollingPolicy(initial_interval_seconds=1, max_interval_seconds=2, timeout_seconds=60, max_attempts=2),
            event_sink=lambda event_type, task: events.append((event_type, task.error)),
        )
        now = datetime(2026, 5, 28, tzinfo=UTC)
        runner.schedule(coordinator.request(self.request()), now)

        retrying = runner.tick(now + timedelta(seconds=1))[0]
        failed = runner.tick(now + timedelta(seconds=3))[0]

        self.assertEqual(retrying.status, "running")
        self.assertIn("provider polling failed", retrying.error)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(events[0][0], "subagent_task_failed")

    def test_lifecycle_runner_fails_task_with_invalid_schedule_timestamp(self) -> None:
        coordinator = self.build_coordinator(SequencedProvider(["completed"]))
        events = []
        runner = SubagentTaskLifecycleRunner(
            coordinator,
            event_sink=lambda event_type, task: events.append((event_type, task.error)),
        )
        task = coordinator.request(self.request())
        coordinator.store.update(task.with_poll_schedule(next_poll_at="not-a-timestamp"))

        failed = runner.tick(datetime(2026, 5, 28, tzinfo=UTC))[0]

        self.assertEqual(failed.status, "failed")
        self.assertIn("next_poll_at", failed.error)
        self.assertEqual(events, [("subagent_task_failed", failed.error)])

    def test_lifecycle_runner_times_out_due_tasks(self) -> None:
        coordinator = self.build_coordinator(SequencedProvider(["running"]))
        events = []
        runner = SubagentTaskLifecycleRunner(
            coordinator,
            policy=PollingPolicy(initial_interval_seconds=1, timeout_seconds=5),
            event_sink=lambda event_type, task: events.append(event_type),
        )
        now = datetime(2026, 5, 28, tzinfo=UTC)
        runner.schedule(coordinator.request(self.request()), now)

        timed_out = runner.tick(now + timedelta(seconds=6))[0]

        self.assertEqual(timed_out.status, "timed_out")
        self.assertEqual(events, ["subagent_task_timed_out"])

    def test_lifecycle_runner_marks_late_completion_after_session_end(self) -> None:
        coordinator = self.build_coordinator(SequencedProvider(["completed"]))
        events = []
        runner = SubagentTaskLifecycleRunner(
            coordinator,
            policy=PollingPolicy(initial_interval_seconds=1, timeout_seconds=60),
            event_sink=lambda event_type, task: events.append(event_type),
            session_active=lambda session_id: False,
        )
        now = datetime(2026, 5, 28, tzinfo=UTC)
        runner.schedule(coordinator.request(self.request()), now)

        runner.tick(now + timedelta(seconds=1))

        self.assertEqual(events, ["subagent_task_late_completed"])

    def test_lifecycle_runner_cancels_pending_tasks_for_session(self) -> None:
        provider = SequencedProvider()
        coordinator = self.build_coordinator(provider)
        events = []
        runner = SubagentTaskLifecycleRunner(
            coordinator,
            event_sink=lambda event_type, task: events.append(event_type),
        )
        task = coordinator.request(self.request())

        cancelled = runner.cancel_session("call-1", "workspace-1")

        self.assertEqual(cancelled[0].task_id, task.task_id)
        self.assertEqual(cancelled[0].status, "cancelled")
        self.assertEqual(provider.cancels, 1)
        self.assertEqual(events, ["subagent_task_cancelled"])

    def test_lifecycle_runner_does_not_call_provider_cancel_when_unsupported(self) -> None:
        provider = SequencedProvider()
        coordinator = SubagentCoordinator()
        coordinator.register(
            provider,
            SubagentProviderDescriptor(
                kind="internal_worker",
                label="Non-cancellable worker",
                supports_cancel=False,
            ),
        )
        events = []
        runner = SubagentTaskLifecycleRunner(
            coordinator,
            event_sink=lambda event_type, task: events.append(event_type),
        )
        task = coordinator.request(self.request())

        cancelled = runner.cancel_session("call-1", "workspace-1")

        self.assertEqual(cancelled[0].task_id, task.task_id)
        self.assertEqual(cancelled[0].status, "cancelled")
        self.assertEqual(provider.cancels, 0)
        self.assertIn("does not support cancellation", cancelled[0].progress_messages[-1])
        self.assertEqual(events, ["subagent_task_cancelled"])

    def test_lifecycle_runner_does_not_poll_non_polling_provider(self) -> None:
        provider = SequencedProvider(["completed"])
        coordinator = SubagentCoordinator()
        coordinator.register(
            provider,
            SubagentProviderDescriptor(
                kind="internal_worker",
                label="Manual worker",
                supports_async_polling=False,
                supports_cancel=False,
            ),
        )
        runner = SubagentTaskLifecycleRunner(
            coordinator,
            policy=PollingPolicy(initial_interval_seconds=1, timeout_seconds=60),
        )
        now = datetime(2026, 5, 28, tzinfo=UTC)
        runner.schedule(coordinator.request(self.request()), now)

        updated = runner.tick(now + timedelta(seconds=1))[0]

        self.assertEqual(provider.polls, 0)
        self.assertEqual(updated.status, "running")
        self.assertIn("does not support async polling", updated.progress_messages[-1])
        self.assertIsNotNone(updated.next_poll_at)

    def test_polling_policy_rejects_invalid_values(self) -> None:
        invalid_policies = [
            {"initial_interval_seconds": 0},
            {"max_interval_seconds": 0},
            {"initial_interval_seconds": 5, "max_interval_seconds": 3},
            {"backoff_multiplier": 0.5},
            {"timeout_seconds": 0},
            {"max_attempts": 0},
        ]

        for kwargs in invalid_policies:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    PollingPolicy(**kwargs)

    def test_lifecycle_snapshot_reports_due_and_overdue_work(self) -> None:
        coordinator = self.build_coordinator(SequencedProvider(["running"]))
        runner = SubagentTaskLifecycleRunner(
            coordinator,
            policy=PollingPolicy(initial_interval_seconds=3, timeout_seconds=10),
        )
        now = datetime(2026, 5, 28, tzinfo=UTC)
        runner.schedule(coordinator.request(self.request()), now)

        waiting = runner.snapshot(now=now + timedelta(seconds=2), workspace_id="workspace-1")
        due = runner.snapshot(now=now + timedelta(seconds=3), workspace_id="workspace-1")
        overdue = runner.snapshot(now=now + timedelta(seconds=11), workspace_id="workspace-1")

        self.assertEqual(waiting["total"], 1)
        self.assertEqual(waiting["pending"], 1)
        self.assertEqual(waiting["due"], 0)
        self.assertEqual(due["due"], 1)
        self.assertEqual(overdue["overdue"], 1)
        self.assertEqual(overdue["status_counts"], {"running": 1})

    def test_lifecycle_snapshot_treats_invalid_schedule_as_overdue(self) -> None:
        coordinator = self.build_coordinator(SequencedProvider(["running"]))
        runner = SubagentTaskLifecycleRunner(coordinator)
        task = coordinator.request(self.request())
        coordinator.store.update(task.with_poll_schedule(next_poll_at="bad-date"))

        snapshot = runner.snapshot(now=datetime(2026, 5, 28, tzinfo=UTC), workspace_id="workspace-1")

        self.assertEqual(snapshot["pending"], 1)
        self.assertEqual(snapshot["due"], 0)
        self.assertEqual(snapshot["overdue"], 1)


if __name__ == "__main__":
    unittest.main()
