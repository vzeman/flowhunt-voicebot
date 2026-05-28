from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
import json

from voicebot.providers import (
    AGENT_CHAT_COMPATIBLE_PROVIDERS,
    SUPPORTED_AGENT_PROVIDERS,
    ProviderCapabilities,
    ProviderDescriptor,
    normalize_provider,
    unsupported_provider_message,
)


AgentRunFactory = Callable[[Any, str, str, float, int, list[dict] | None], tuple[str, list[dict]]]


@dataclass
class AgentProviderRegistry:
    factories: dict[str, AgentRunFactory] = field(default_factory=dict)
    descriptors: dict[str, ProviderDescriptor] = field(default_factory=dict)

    def register(
        self,
        provider: str,
        factory: AgentRunFactory,
        descriptor: ProviderDescriptor | None = None,
    ) -> None:
        normalized = normalize_provider(provider)
        self.factories[normalized] = factory
        self.descriptors[normalized] = descriptor or default_agent_descriptor(normalized)

    def describe(self, provider: str) -> ProviderDescriptor | None:
        return self.descriptors.get(normalize_provider(provider))

    def catalog(self) -> dict[str, dict]:
        return {
            "providers": {
                provider: descriptor.to_dict()
                for provider, descriptor in sorted(self.descriptors.items())
            }
        }

    def run(
        self,
        client: Any,
        provider: str,
        model: str,
        prompt: str,
        timeout: float,
        max_output_tokens: int,
        tools: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        provider = normalize_provider(provider)
        factory = self.factories.get(provider)
        if factory is not None:
            return factory(client, model, prompt, timeout, max_output_tokens, tools)
        if provider in SUPPORTED_AGENT_PROVIDERS:
            raise RuntimeError(
                unsupported_provider_message(
                    "agent",
                    provider,
                    SUPPORTED_AGENT_PROVIDERS,
                    "Use VOICEBOT_AGENT_PROVIDER=openai-chat-compatible with VOICEBOT_AGENT_OPENAI_BASE_URL, "
                    "VOICEBOT_AGENT_API_KEY, and VOICEBOT_OPENAI_AGENT_MODEL until a native adapter is added.",
                )
            )
        raise RuntimeError(f"unsupported agent provider: {provider}")


def default_agent_provider_registry() -> AgentProviderRegistry:
    registry = AgentProviderRegistry()
    registry.register("anthropic", run_anthropic_agent)
    registry.register("openai-responses", run_responses_agent)
    for provider in AGENT_CHAT_COMPATIBLE_PROVIDERS:
        registry.register(provider, run_chat_agent)
    return registry


def default_agent_descriptor(provider: str) -> ProviderDescriptor:
    adapter = "chat_compatible"
    native_tools = False
    if provider in {"anthropic", "openai-responses"}:
        adapter = "native"
        native_tools = True
    return ProviderDescriptor(
        provider=provider,
        family="agent",
        adapter=adapter,
        capabilities=ProviderCapabilities(
            modalities=frozenset({"agent"}),
            required_credentials=("api_key",),
            latency_profile="interactive",
            usage_metadata=("input_tokens", "output_tokens", "tool_calls") if native_tools else ("input_tokens", "output_tokens"),
            native_tools=native_tools,
        ),
    )


def run_responses_agent(
    client: Any,
    model: str,
    prompt: str,
    timeout: float,
    max_output_tokens: int,
    tools: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    kwargs = {
        "model": model,
        "input": prompt,
        "max_output_tokens": max_output_tokens,
        "timeout": timeout,
    }
    if tools:
        kwargs["tools"] = tools
    response = client.responses.create(**kwargs)
    return response.output_text.strip(), extract_responses_tool_calls(response)


def run_chat_agent(
    client: Any,
    model: str,
    prompt: str,
    timeout: float,
    max_output_tokens: int,
    tools: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_output_tokens,
        temperature=0,
        timeout=timeout,
    )
    return (response.choices[0].message.content or "").strip(), []


def run_anthropic_agent(
    client: Any,
    model: str,
    prompt: str,
    timeout: float,
    max_output_tokens: int,
    tools: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_output_tokens,
        "temperature": 0,
        "timeout": timeout,
    }
    converted_tools = anthropic_tools_from_json_schema(tools or [])
    if converted_tools:
        kwargs["tools"] = converted_tools
    response = client.messages.create(**kwargs)
    return extract_anthropic_text(response), extract_anthropic_tool_calls(response)


def anthropic_tools_from_json_schema(tools: list[dict]) -> list[dict]:
    converted = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        name = tool.get("name")
        if not name:
            continue
        converted.append(
            {
                "name": name,
                "description": tool.get("description", ""),
                "input_schema": tool.get(
                    "parameters",
                    {"type": "object", "properties": {}, "additionalProperties": False},
                ),
            }
        )
    return converted


def extract_anthropic_text(response: Any) -> str:
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def extract_anthropic_tool_calls(response: Any) -> list[dict]:
    calls = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) != "tool_use":
            continue
        name = getattr(block, "name", "")
        arguments = getattr(block, "input", {}) or {}
        if name:
            calls.append({"name": name, "arguments": arguments})
    return calls


def extract_responses_tool_calls(response: Any) -> list[dict]:
    calls = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "function_call":
            continue
        name = getattr(item, "name", "")
        raw_arguments = getattr(item, "arguments", "{}") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError:
            arguments = {}
        if name:
            calls.append({"name": name, "arguments": arguments})
    return calls
