from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class DependabotAutomationTests(unittest.TestCase):
    def test_dependabot_tracks_python_and_github_actions_updates(self) -> None:
        config = read_repo_file(".github/dependabot.yml")

        self.assertIn("version: 2", config)
        self.assertRegex(config, r"package-ecosystem:\s+pip")
        self.assertRegex(config, r"package-ecosystem:\s+github-actions")
        self.assertIn("interval: weekly", config)
        self.assertIn("python-runtime:", config)
        self.assertIn("github-actions:", config)

    def test_dependabot_automerge_is_scoped_and_waits_for_required_checks(self) -> None:
        workflow = read_repo_file(".github/workflows/dependabot-automerge.yml")

        self.assertIn("pull_request_target:", workflow)
        self.assertIn("github.actor == 'dependabot[bot]'", workflow)
        self.assertIn("dependabot/fetch-metadata", workflow)
        self.assertRegex(workflow, r"gh pr merge .+ --auto --squash --delete-branch")
        self.assertIn("contents: write", workflow)
        self.assertIn("pull-requests: write", workflow)

    def test_validation_workflow_keeps_dependency_update_safety_gates(self) -> None:
        workflow = read_repo_file(".github/workflows/validation.yml")

        forbidden_pattern = "|".join(("PIPE" + "CAT", "Pipe" + "cat", "pipe" + "cat"))
        required_commands = [
            "python -m unittest discover -s tests",
            'python -m pytest -q -m "not e2e"',
            "python -m py_compile",
            "git diff --check",
            f'rg -n "{forbidden_pattern}"',
            "SECRET_PATTERN=",
        ]
        for command in required_commands:
            self.assertIn(command, workflow)

    def test_dependabot_runbook_documents_failed_update_handling(self) -> None:
        runbook = read_repo_file("docs/DEPENDABOT_AUTOMATION.md")

        self.assertIn("Dependabot", runbook)
        self.assertIn("not merged automatically", runbook)
        self.assertIn("deployed e2e", runbook.lower())
        self.assertIsNotNone(re.search(r"pytest.+not e2e", runbook))


if __name__ == "__main__":
    unittest.main()
