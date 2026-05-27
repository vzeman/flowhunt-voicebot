from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from agent_provider_registry import AgentProviderRegistry, default_agent_provider_registry


class FakeResponses:
    def __init__(self) -> None:
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            output_text=" done ",
            output=[
                SimpleNamespace(type="function_call", name="hangup_call", arguments='{"call_id":"call-1"}'),
                SimpleNamespace(type="message", name="ignored", arguments="{}"),
            ],
        )


class FakeChatCompletions:
    def __init__(self) -> None:
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="chat answer"))])


class FakeClient:
    def __init__(self) -> None:
        self.responses = FakeResponses()
        self.chat = SimpleNamespace(completions=FakeChatCompletions())


class AgentProviderRegistryTests(unittest.TestCase):
    def test_registry_runs_custom_provider(self) -> None:
        registry = AgentProviderRegistry()
        registry.register("custom_agent", lambda client, model, prompt, timeout, max_tokens, tools: ("ok", []))

        answer, tool_calls = registry.run(object(), "custom_agent", "model", "prompt", 1.0, 100)

        self.assertEqual(answer, "ok")
        self.assertEqual(tool_calls, [])

    def test_default_registry_runs_responses_provider_with_tools(self) -> None:
        registry = default_agent_provider_registry()
        client = FakeClient()

        answer, tool_calls = registry.run(
            client,
            "openai-responses",
            "model-a",
            "prompt",
            2.0,
            100,
            [{"type": "function", "name": "hangup_call"}],
        )

        self.assertEqual(answer, "done")
        self.assertEqual(tool_calls, [{"name": "hangup_call", "arguments": {"call_id": "call-1"}}])
        self.assertEqual(client.responses.kwargs["model"], "model-a")
        self.assertEqual(client.responses.kwargs["tools"], [{"type": "function", "name": "hangup_call"}])

    def test_default_registry_runs_chat_compatible_provider(self) -> None:
        registry = default_agent_provider_registry()
        client = FakeClient()

        answer, tool_calls = registry.run(client, "openai-chat-compatible", "model-a", "prompt", 2.0, 100)

        self.assertEqual(answer, "chat answer")
        self.assertEqual(tool_calls, [])
        self.assertEqual(client.chat.completions.kwargs["messages"], [{"role": "user", "content": "prompt"}])

    def test_registry_reports_known_but_unimplemented_agent_provider(self) -> None:
        registry = AgentProviderRegistry()

        with self.assertRaisesRegex(RuntimeError, "Unsupported agent provider adapter for 'anthropic'"):
            registry.run(object(), "anthropic", "model", "prompt", 1.0, 100)


if __name__ == "__main__":
    unittest.main()
