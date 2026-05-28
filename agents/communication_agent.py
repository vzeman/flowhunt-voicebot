from __future__ import annotations

from dataclasses import dataclass
import os
import time
import urllib.error
from typing import Any

from agent_provider_registry import AgentProviderRegistry, default_agent_provider_registry
from local_command_agent import (
    attach_response_event_id,
    build_prompt,
    build_retry_prompt,
    build_tool_result_prompt,
    ClaimRenewer,
    claim_tasks,
    execute_tool_call,
    execute_tool_calls,
    fast_tool_call,
    filter_voice_agent_tools,
    http_json,
    is_echo_answer,
    needs_spoken_followup,
    parse_agent_output,
    remove_colleague_reentrant_tool_calls,
    release_tasks,
)


@dataclass(frozen=True)
class CommunicationAgentConfig:
    base_url: str
    provider: str
    model: str
    interval: float
    timeout: float
    max_output_tokens: int
    owner_prefix: str
    echo_error_label: str = "communication agent"


def run_communication_agent(
    client: Any,
    config: CommunicationAgentConfig,
    agent_providers: AgentProviderRegistry | None = None,
) -> None:
    providers = agent_providers or default_agent_provider_registry()
    owner = f"{config.owner_prefix}:{os.getpid()}"
    seen: set[int] = set()
    claimed_pending: list[dict] = []

    while True:
        try:
            claimed_pending = []
            active_call_ids = set(http_json("GET", f"{config.base_url}/health").get("active_calls", []))
            response = http_json("GET", f"{config.base_url}/agent/tasks")
            pending = [
                task
                for task in response.get("pending", [])
                if task["id"] not in seen and task.get("call_id") in active_call_ids
            ]
            if not pending:
                time.sleep(config.interval)
                continue

            pending = claim_tasks(config.base_url, pending, owner, max(config.timeout * 2, 30.0))
            if not pending:
                time.sleep(config.interval)
                continue
            claimed_pending = pending

            ttl_seconds = max(config.timeout * 2, 30.0)
            with ClaimRenewer(config.base_url, pending, owner, ttl_seconds):
                latest = pending[-1]
                deterministic_call = fast_tool_call(latest)
                if deterministic_call:
                    execute_tool_call(config.base_url, deterministic_call)
                    seen.add(latest["id"])
                    print(
                        f"executed deterministic tool {deterministic_call['name']} for event {latest['id']}",
                        flush=True,
                    )
                    claimed_pending = []
                    continue

                legacy_tools = filter_voice_agent_tools(http_json("GET", f"{config.base_url}/agent/tools").get("tools", []))
                native_tools = filter_voice_agent_tools(
                    http_json("GET", f"{config.base_url}/agent/tools/schema").get("tools", [])
                )
                prompt = build_prompt(pending, response.get("context", {}), legacy_tools)
                answer, tool_calls = run_model_turn(client, providers, config, prompt, pending, native_tools)

                tool_calls = attach_response_event_id(tool_calls, latest["id"])
                tool_calls = remove_colleague_reentrant_tool_calls(pending, tool_calls)
                tool_results = execute_tool_calls(config.base_url, tool_calls)
                if tool_calls and needs_spoken_followup(tool_calls):
                    follow_up_prompt = build_tool_result_prompt(prompt, tool_results)
                    raw_answer, _native_tool_calls = providers.run(
                        client,
                        config.provider,
                        config.model,
                        follow_up_prompt,
                        config.timeout,
                        config.max_output_tokens,
                        None,
                    )
                    answer, _parsed_tool_calls = parse_agent_output(raw_answer)
                if answer:
                    execute_tool_call(
                        config.base_url,
                        {
                            "name": "say",
                            "arguments": {
                                "call_id": latest["call_id"],
                                "text": answer,
                                "response_to_event_id": latest["id"],
                            },
                        },
                    )
                elif tool_results and needs_spoken_followup(tool_calls):
                    execute_tool_call(
                        config.base_url,
                        {
                            "name": "say",
                            "arguments": {
                                "call_id": latest["call_id"],
                                "text": "I asked a FlowHunt colleague to check that and I am waiting for the result.",
                                "response_to_event_id": latest["id"],
                            },
                        },
                    )
            for task in pending:
                seen.add(task["id"])
            claimed_pending = []
            print(f"answered {len(pending)} pending event(s) for call {latest['call_id']}", flush=True)
        except (OSError, urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            if claimed_pending:
                try:
                    release_tasks(config.base_url, claimed_pending, owner)
                except (OSError, urllib.error.URLError, TimeoutError, RuntimeError):
                    pass
                claimed_pending = []
            print(f"agent error: {exc}", flush=True)
            time.sleep(max(config.interval, 2.0))


def run_model_turn(
    client: Any,
    providers: AgentProviderRegistry,
    config: CommunicationAgentConfig,
    prompt: str,
    pending: list[dict],
    native_tools: list[dict],
) -> tuple[str, list[dict]]:
    raw_answer, native_tool_calls = providers.run(
        client,
        config.provider,
        config.model,
        prompt,
        config.timeout,
        config.max_output_tokens,
        native_tools,
    )
    answer, parsed_tool_calls = parse_agent_output(raw_answer)
    tool_calls = [*native_tool_calls, *parsed_tool_calls]
    if answer and is_echo_answer(answer, pending):
        retry_prompt = build_retry_prompt(prompt, answer)
        raw_answer, native_tool_calls = providers.run(
            client,
            config.provider,
            config.model,
            retry_prompt,
            config.timeout,
            config.max_output_tokens,
            native_tools,
        )
        answer, parsed_tool_calls = parse_agent_output(raw_answer)
        tool_calls = [*native_tool_calls, *parsed_tool_calls]
        if answer and is_echo_answer(answer, pending):
            raise RuntimeError(f"{config.echo_error_label} returned echo response twice: {answer}")
    return answer, tool_calls
