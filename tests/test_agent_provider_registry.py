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


class FakeAnthropicMessages:
    def __init__(self) -> None:
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text=" anthropic answer "),
                SimpleNamespace(type="tool_use", name="transfer_call", input={"call_id": "call-1", "target": "123"}),
            ],
        )


class FakeAnthropicClient:
    def __init__(self) -> None:
        self.messages = FakeAnthropicMessages()


class AgentProviderRegistryTests(unittest.TestCase):
    def test_registry_runs_custom_provider(self) -> None:
        registry = AgentProviderRegistry()
        registry.register("custom_agent", lambda client, model, prompt, timeout, max_tokens, tools: ("ok", []))

        answer, tool_calls = registry.run(object(), "custom_agent", "model", "prompt", 1.0, 100)

        self.assertEqual(answer, "ok")
        self.assertEqual(tool_calls, [])
        self.assertEqual(registry.describe("custom_agent").family, "agent")

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
        self.assertTrue(registry.describe("openai-responses").capabilities.native_tools)

    def test_default_registry_runs_chat_compatible_provider(self) -> None:
        registry = default_agent_provider_registry()
        client = FakeClient()

        answer, tool_calls = registry.run(client, "openai-chat-compatible", "model-a", "prompt", 2.0, 100)

        self.assertEqual(answer, "chat answer")
        self.assertEqual(tool_calls, [])
        self.assertEqual(client.chat.completions.kwargs["messages"], [{"role": "user", "content": "prompt"}])

    def test_default_registry_runs_anthropic_provider_with_tools(self) -> None:
        registry = default_agent_provider_registry()
        client = FakeAnthropicClient()

        answer, tool_calls = registry.run(
            client,
            "anthropic",
            "claude-test",
            "prompt",
            2.0,
            100,
            [
                {
                    "type": "function",
                    "name": "transfer_call",
                    "description": "Transfer call.",
                    "parameters": {
                        "type": "object",
                        "properties": {"call_id": {"type": "string"}, "target": {"type": "string"}},
                        "required": ["call_id", "target"],
                    },
                }
            ],
        )

        self.assertEqual(answer, "anthropic answer")
        self.assertEqual(tool_calls, [{"name": "transfer_call", "arguments": {"call_id": "call-1", "target": "123"}}])
        self.assertEqual(client.messages.kwargs["model"], "claude-test")
        self.assertEqual(client.messages.kwargs["tools"][0]["name"], "transfer_call")
        self.assertEqual(client.messages.kwargs["tools"][0]["input_schema"]["required"], ["call_id", "target"])

    def test_registry_reports_known_but_unimplemented_agent_provider(self) -> None:
        registry = AgentProviderRegistry()

        with self.assertRaisesRegex(RuntimeError, "Unsupported agent provider adapter for 'gemini'"):
            registry.run(object(), "gemini", "model", "prompt", 1.0, 100)

    def test_registry_catalog_exposes_agent_provider_capabilities(self) -> None:
        registry = default_agent_provider_registry()

        catalog = registry.catalog()

        self.assertIn("anthropic", catalog["providers"])
        self.assertEqual(catalog["providers"]["anthropic"]["family"], "agent")
        self.assertEqual(catalog["providers"]["openai-chat-compatible"]["adapter"], "chat_compatible")


if __name__ == "__main__":
    unittest.main()
