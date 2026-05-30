from __future__ import annotations

from dataclasses import dataclass
import os
import threading
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
    answer_as_say_call,
    ensure_action_acknowledgements,
    execute_conversational_tool_calls,
    execute_tool_call,
    fast_tool_calls,
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
                deterministic_calls = fast_tool_calls(latest)
                if deterministic_calls:
                    execute_conversational_tool_calls(config.base_url, deterministic_calls)
                    seen.add(latest["id"])
                    print(
                        f"executed {len(deterministic_calls)} deterministic tool(s) for event {latest['id']}",
                        flush=True,
                    )
                    claimed_pending = []
                    continue

                legacy_tools = filter_voice_agent_tools(http_json("GET", f"{config.base_url}/agent/tools").get("tools", []))
                native_tools = filter_voice_agent_tools(
                    http_json("GET", f"{config.base_url}/agent/tools/schema").get("tools", [])
                )
                prompt = build_prompt(pending, response.get("context", {}), legacy_tools)
                delayed_ack = DelayedProgressAcknowledgement(config.base_url, latest)
                delayed_ack.start()
                try:
                    answer, tool_calls = run_model_turn(client, providers, config, prompt, pending, native_tools)
                except Exception as exc:
                    answer = provider_failure_answer(exc)
                    tool_calls = []
                    print(f"provider turn failed for event {latest['id']}: {exc}", flush=True)
                finally:
                    delayed_ack.stop()

                tool_calls = attach_response_event_id(tool_calls, latest["id"])
                if delayed_ack.delivered:
                    tool_calls = suppress_colleague_tool_progress(tool_calls)
                tool_calls = remove_colleague_reentrant_tool_calls(pending, tool_calls)
                tool_calls = ensure_action_acknowledgements(tool_calls)
                initial_say = answer_as_say_call(answer, latest)
                if delayed_ack.delivered and has_colleague_tool_call(tool_calls):
                    initial_say = None
                    answer = ""
                calls_for_initial_execution = list(tool_calls)
                if initial_say and tool_calls and not needs_spoken_followup(tool_calls):
                    calls_for_initial_execution = [initial_say, *tool_calls]
                    answer = ""
                tool_results = execute_conversational_tool_calls(config.base_url, calls_for_initial_execution)
                if tool_calls and needs_spoken_followup(tool_calls):
                    follow_up_prompt = build_tool_result_prompt(prompt, tool_results)
                    try:
                        raw_answer, _native_tool_calls = run_provider_with_retry(
                            client,
                            providers,
                            config,
                            follow_up_prompt,
                            None,
                        )
                        answer, _parsed_tool_calls = parse_agent_output(raw_answer)
                    except Exception as exc:
                        answer = provider_failure_answer(exc)
                        print(f"provider follow-up failed for event {latest['id']}: {exc}", flush=True)
                if answer:
                    tool_results.extend(
                        execute_conversational_tool_calls(config.base_url, [answer_as_say_call(answer, latest)])
                    )
                elif tool_results and needs_spoken_followup(tool_calls):
                    tool_results.extend(
                        execute_conversational_tool_calls(
                            config.base_url,
                            [
                                {
                                    "name": "say",
                                    "arguments": {
                                        "call_id": latest["call_id"],
                                        "text": "I asked a FlowHunt colleague to check that and I am waiting for the result.",
                                        "response_to_event_id": latest["id"],
                                    },
                                }
                            ],
                        )
                    )
                if has_http_failed_say(tool_results):
                    release_tasks(config.base_url, pending, owner)
                    claimed_pending = []
                    print(f"released {len(pending)} pending event(s) after failed speech delivery", flush=True)
                    continue
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
    raw_answer, native_tool_calls = run_provider_with_retry(client, providers, config, prompt, native_tools)
    answer, parsed_tool_calls = parse_agent_output(raw_answer)
    tool_calls = [*native_tool_calls, *parsed_tool_calls]
    if answer and is_echo_answer(answer, pending):
        retry_prompt = build_retry_prompt(prompt, answer)
        raw_answer, native_tool_calls = run_provider_with_retry(client, providers, config, retry_prompt, native_tools)
        answer, parsed_tool_calls = parse_agent_output(raw_answer)
        tool_calls = [*native_tool_calls, *parsed_tool_calls]
        if answer and is_echo_answer(answer, pending):
            raise RuntimeError(f"{config.echo_error_label} returned echo response twice: {answer}")
    return answer, tool_calls


def run_provider_with_retry(
    client: Any,
    providers: AgentProviderRegistry,
    config: CommunicationAgentConfig,
    prompt: str,
    native_tools: list[dict] | None,
) -> tuple[str, list[dict]]:
    try:
        return providers.run(
            client,
            config.provider,
            config.model,
            prompt,
            config.timeout,
            config.max_output_tokens,
            native_tools,
        )
    except Exception as exc:
        _ = exc
        time.sleep(min(0.5, max(config.interval, 0.05)))
        return providers.run(
            client,
            config.provider,
            config.model,
            prompt,
            config.timeout,
            config.max_output_tokens,
            native_tools,
        )


def provider_failure_answer(exc: Exception) -> str:
    _ = exc
    return "I had a temporary AI error. Please repeat that once more."


def has_http_failed_say(results: list[dict]) -> bool:
    return any(result.get("name") == "say" and not result.get("ok") for result in results)


class DelayedProgressAcknowledgement:
    def __init__(self, base_url: str, task: dict, delay_seconds: float = 1.8) -> None:
        self.base_url = base_url
        self.task = task
        self.delay_seconds = delay_seconds
        self.delivered = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        if not should_send_delayed_acknowledgement(self.task):
            return
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=0.2)

    def _run(self) -> None:
        if self._stop.wait(self.delay_seconds):
            return
        try:
            http_json(
                "POST",
                f"{self.base_url}/calls/{self.task['call_id']}/responses",
                {
                    "text": "Give me a moment.",
                    "response_to_event_id": None,
                    "response_kind": "progress_ack",
                },
            )
            self.delivered = True
        except (OSError, urllib.error.URLError, TimeoutError, RuntimeError):
            return


def should_send_delayed_acknowledgement(task: dict) -> bool:
    reason = str(task.get("data", {}).get("reason") or "")
    return reason not in {"call_connected", "colleague_result", "colleague_progress"}


def suppress_colleague_tool_progress(tool_calls: list[dict]) -> list[dict]:
    suppressed = []
    for call in tool_calls:
        if call.get("name") in {"delegate_to_subagent", "invoke_flowhunt_flow", "create_flowhunt_project_issue"}:
            arguments = call.setdefault("arguments", {})
            if isinstance(arguments, dict):
                arguments["suppress_progress"] = True
        suppressed.append(call)
    return suppressed


def has_colleague_tool_call(tool_calls: list[dict]) -> bool:
    colleague_tools = {"delegate_to_subagent", "invoke_flowhunt_flow", "create_flowhunt_project_issue"}
    return any(call.get("name") in colleague_tools for call in tool_calls)
