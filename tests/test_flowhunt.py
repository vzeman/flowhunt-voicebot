from __future__ import annotations

import unittest

from voicebot.flowhunt import (
    FlowHuntClient,
    FlowHuntResult,
    extract_flow_result_from_events,
    extract_flow_task_id,
    extract_flow_task_result,
    extract_issue_result,
    extract_issue_updates,
    is_flow_task_terminal,
    is_terminal_issue_state,
)


class FlowHuntParsingTests(unittest.TestCase):
    def test_interim_messages_are_updates_not_final_result(self) -> None:
        issue = {
            "status": "open",
            "messages": [
                {"content": "I am checking the account details."},
                {"content": "Still waiting for one API response."},
            ],
        }

        self.assertEqual(extract_issue_result(issue), "")
        self.assertEqual(
            extract_issue_updates(issue),
            "I am checking the account details.\nStill waiting for one API response.",
        )

    def test_final_result_is_extracted_as_result(self) -> None:
        issue = {"status": "completed", "result": "The refund was created."}

        self.assertEqual(extract_issue_result(issue), "The refund was created.")

    def test_human_input_needed_is_terminal(self) -> None:
        self.assertTrue(is_terminal_issue_state("human_input_needed"))

    def test_flow_result_is_extracted_from_ai_message_event(self) -> None:
        events = [
            {"event_type": "system", "metadata": {"message": "loading"}},
            {
                "event_type": "ai",
                "action_type": "message",
                "metadata": {"message": "The sitemap contains 1,950 pages."},
            },
        ]

        self.assertEqual(extract_flow_result_from_events(events), "The sitemap contains 1,950 pages.")

    def test_empty_flow_task_payload_is_not_treated_as_result(self) -> None:
        task = {
            "result": '{"outputs":[],"credit_usage":[],"ai_answer":null,"credits":0.0,"status":"error","error_message":null}',
        }

        self.assertEqual(extract_flow_task_result(task), "")

    def test_pending_flow_task_with_empty_inner_error_keeps_polling(self) -> None:
        task = {
            "id": "task-1",
            "status": "PENDING",
            "result": '{"outputs":[],"credit_usage":[],"ai_answer":null,"credits":0.0,"status":"error","error_message":null}',
        }

        self.assertFalse(is_flow_task_terminal(task))

    def test_pending_flow_task_enum_status_keeps_polling(self) -> None:
        task = {
            "id": "task-1",
            "status": "TaskStatus.PENDING",
            "result": '{"outputs":[],"credit_usage":[],"ai_answer":null,"credits":0.0,"status":"error","error_message":null}',
        }

        self.assertFalse(is_flow_task_terminal(task))

    def test_completed_flow_task_enum_status_is_terminal(self) -> None:
        task = {
            "id": "task-1",
            "status": "TaskStatus.SUCCESS",
            "result": '{"ai_answer":"LiveAgent supports IVR.","status":"success"}',
        }

        self.assertTrue(is_flow_task_terminal(task))
        self.assertEqual(extract_flow_task_result(task), "LiveAgent supports IVR.")

    def test_unknown_outer_flow_task_status_keeps_polling(self) -> None:
        task = {
            "id": "task-1",
            "status": "TaskStatus.WAITING_FOR_WORKER",
            "result": '{"outputs":[],"ai_answer":null,"status":"error","error_message":null}',
        }

        self.assertFalse(is_flow_task_terminal(task))

    def test_flow_task_id_uses_invoke_task_id(self) -> None:
        self.assertEqual(extract_flow_task_id({"id": "invoke-task-1"}), "invoke-task-1")
        self.assertEqual(extract_flow_task_id({"task_id": "explicit-task-1", "id": "other"}), "explicit-task-1")

    def test_wait_for_flow_task_polls_v2_task_until_result(self) -> None:
        class PollingClient(FlowHuntClient):
            def __init__(self) -> None:
                super().__init__("key", "workspace")
                self.polled: list[tuple[str, str]] = []

            def get_flow_task(self, flow_id: str, task_id: str) -> FlowHuntResult:
                self.polled.append((flow_id, task_id))
                return FlowHuntResult(
                    True,
                    "ok",
                    {
                        "response": {
                            "id": task_id,
                            "status": "SUCCESS",
                            "result": '{"ai_answer":"The sitemap has 1,950 pages.","status":"success"}',
                        }
                    },
                )

        client = PollingClient()
        initial_task = {
            "id": "task-1",
            "status": "PENDING",
            "result": '{"outputs":[],"credit_usage":[],"ai_answer":null,"credits":0.0,"status":"error","error_message":null}',
        }

        result = client._wait_for_flow_task("flow-1", initial_task, wait_seconds=1.0, poll_interval_seconds=0.01)

        self.assertTrue(result.ok)
        self.assertEqual(result.message, "The sitemap has 1,950 pages.")
        self.assertEqual(client.polled, [("flow-1", "task-1")])


if __name__ == "__main__":
    unittest.main()
