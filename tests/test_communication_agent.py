from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from agent_provider_registry import AgentProviderRegistry
from communication_agent import (
    CommunicationAgentConfig,
    has_http_failed_say,
    provider_failure_answer,
    run_provider_with_retry,
)


class CommunicationAgentProviderRecoveryTests(unittest.TestCase):
    def make_config(self) -> CommunicationAgentConfig:
        return CommunicationAgentConfig(
            base_url="http://voicebot",
            provider="test",
            model="model",
            interval=0.01,
            timeout=1.0,
            max_output_tokens=80,
            owner_prefix="test-agent",
        )

    def test_provider_call_is_retried_once(self) -> None:
        calls = 0

        def flaky_provider(client, model, prompt, timeout, max_output_tokens, tools):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise Exception("temporary provider failure")
            return "ok", []

        registry = AgentProviderRegistry()
        registry.register("test", flaky_provider)

        answer, tool_calls = run_provider_with_retry(object(), registry, self.make_config(), "prompt", [])

        self.assertEqual(answer, "ok")
        self.assertEqual(tool_calls, [])
        self.assertEqual(calls, 2)

    def test_provider_failure_answer_is_short_and_spoken(self) -> None:
        self.assertEqual(
            provider_failure_answer(Exception("server_error")),
            "I had a temporary AI error. Please repeat that once more.",
        )

    def test_provider_server_error_is_not_retried_for_realtime_turn(self) -> None:
        calls = 0

        def failing_provider(client, model, prompt, timeout, max_output_tokens, tools):
            nonlocal calls
            calls += 1
            raise Exception("Error code: 500 - server_error")

        registry = AgentProviderRegistry()
        registry.register("test", failing_provider)

        with self.assertRaisesRegex(Exception, "server_error"):
            run_provider_with_retry(object(), registry, self.make_config(), "prompt", [])

        self.assertEqual(calls, 1)

    def test_failed_say_http_result_is_detected(self) -> None:
        self.assertTrue(has_http_failed_say([{"name": "say", "ok": False, "error": "HTTP Error 404"}]))
        self.assertFalse(has_http_failed_say([{"name": "say", "ok": True, "result": {"ok": False}}]))


if __name__ == "__main__":
    unittest.main()
