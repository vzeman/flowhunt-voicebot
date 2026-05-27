from __future__ import annotations

from unittest.mock import patch
import unittest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from local_command_agent import claim_tasks, release_tasks, renew_tasks


class AgentClaimClientTests(unittest.TestCase):
    def test_claim_tasks_returns_only_claimed_tasks(self) -> None:
        tasks = [
            {"id": 1, "call_id": "call-1"},
            {"id": 2, "call_id": "call-1"},
        ]

        with patch("local_command_agent.http_json") as http_json:
            http_json.return_value = {"claimed_event_ids": [2]}
            claimed = claim_tasks("http://voicebot", tasks, "worker-1", 30)

        self.assertEqual(claimed, [tasks[1]])
        http_json.assert_called_once_with(
            "POST",
            "http://voicebot/agent/tasks/claim",
            {"event_ids": [1, 2], "owner": "worker-1", "ttl_seconds": 30},
        )

    def test_claim_tasks_skips_empty_task_list(self) -> None:
        with patch("local_command_agent.http_json") as http_json:
            claimed = claim_tasks("http://voicebot", [], "worker-1", 30)

        self.assertEqual(claimed, [])
        http_json.assert_not_called()

    def test_release_tasks_posts_claimed_event_ids(self) -> None:
        tasks = [
            {"id": 1, "call_id": "call-1"},
            {"id": 2, "call_id": "call-1"},
        ]

        with patch("local_command_agent.http_json") as http_json:
            http_json.return_value = {"released_event_ids": [1, 2]}
            response = release_tasks("http://voicebot", tasks, "worker-1")

        self.assertEqual(response, {"released_event_ids": [1, 2]})
        http_json.assert_called_once_with(
            "POST",
            "http://voicebot/agent/tasks/release",
            {"event_ids": [1, 2], "owner": "worker-1"},
        )

    def test_renew_tasks_posts_claimed_event_ids(self) -> None:
        tasks = [
            {"id": 1, "call_id": "call-1"},
            {"id": 2, "call_id": "call-1"},
        ]

        with patch("local_command_agent.http_json") as http_json:
            http_json.return_value = {"renewed_event_ids": [1, 2], "owner": "worker-1"}
            response = renew_tasks("http://voicebot", tasks, "worker-1", 45)

        self.assertEqual(response, {"renewed_event_ids": [1, 2], "owner": "worker-1"})
        http_json.assert_called_once_with(
            "POST",
            "http://voicebot/agent/tasks/renew",
            {"event_ids": [1, 2], "owner": "worker-1", "ttl_seconds": 45},
        )


if __name__ == "__main__":
    unittest.main()
