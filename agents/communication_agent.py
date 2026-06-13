from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import threading
import time
import urllib.error
from typing import Any

from agent_provider_registry import AgentProviderRegistry, default_agent_provider_registry
from local_command_agent import (
    attach_task_context,
    agent_tasks_url,
    build_prompt,
    build_retry_prompt,
    build_tool_result_prompt,
    ClaimRenewer,
    claim_tasks,
    answer_as_say_call,
    chat_mode_for_task,
    ensure_action_acknowledgements,
    execute_conversational_tool_calls,
    execute_tool_call,
    fast_tool_calls,
    filter_grounded_call_control_tools,
    filter_voice_agent_tools,
    http_json,
    is_echo_answer,
    needs_spoken_followup,
    parse_agent_output,
    parse_agent_output_with_chat,
    ensure_expanded_chat_for_say_calls,
    remove_colleague_reentrant_tool_calls,
    release_tasks,
    speculative_progress_answer,
    suppress_duplicate_colleague_tool_calls_for_speculative,
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
    streaming_enabled: bool = False
    streaming_chunk_chars: int = 90
    progress_ack_delay_seconds: float = 2.0


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
            response = http_json("GET", agent_tasks_url(config.base_url, max(config.interval, 5.0)))
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
                streamed_response = False
                chat = None
                try:
                    if config.streaming_enabled:
                        answer, tool_calls, streamed_response = run_model_turn_streaming(
                            client,
                            providers,
                            config,
                            prompt,
                            pending,
                            native_tools,
                            latest,
                        )
                    else:
                        answer, tool_calls, chat = run_model_turn(client, providers, config, prompt, pending, native_tools)
                except Exception as exc:
                    answer = provider_failure_answer(exc)
                    tool_calls = []
                    chat = None
                    print(f"provider turn failed for event {latest['id']}: {exc}", flush=True)

                model_requested_tools = bool(tool_calls)
                tool_calls = attach_task_context(tool_calls, latest)
                tool_calls = filter_grounded_call_control_tools(
                    tool_calls,
                    latest,
                    lambda task, call: validate_call_control_tool(client, providers, config, task, call),
                )
                if not tool_calls and not answer.strip() and model_requested_tools:
                    answer = fallback_answer_for_dropped_tools(latest)
                if not tool_calls and answer:
                    tool_calls = recover_missing_colleague_tool_call(
                        client,
                        providers,
                        config,
                        latest,
                        response.get("context", {}),
                        answer,
                        native_tools,
                    )
                    if tool_calls:
                        print(f"recovered missing colleague tool call for event {latest['id']}", flush=True)
                tool_calls = remove_colleague_reentrant_tool_calls(pending, tool_calls)
                tool_calls, suppressed_speculative_duplicate = suppress_duplicate_colleague_tool_calls_for_speculative(
                    latest,
                    tool_calls,
                )
                if suppressed_speculative_duplicate and not answer.strip():
                    answer = speculative_progress_answer(latest)
                tool_calls = ensure_action_acknowledgements(tool_calls)
                initial_say = None if streamed_response else answer_as_say_call(answer, latest, chat=chat)
                if should_prepend_colleague_progress_ack(
                    latest,
                    tool_calls,
                    initial_say=initial_say,
                    delayed_ack_delivered=False,
                    streamed_response=streamed_response,
                ):
                    tool_calls = suppress_colleague_tool_progress(tool_calls)
                    calls_for_initial_execution = [colleague_progress_ack_tool_call(latest), *tool_calls]
                else:
                    calls_for_initial_execution = list(tool_calls)
                calls_for_initial_execution = ensure_expanded_chat_for_say_calls(calls_for_initial_execution, latest)
                if initial_say and tool_calls and not needs_spoken_followup(tool_calls):
                    calls_for_initial_execution = [initial_say, *tool_calls]
                    answer = ""
                    calls_for_initial_execution = ensure_expanded_chat_for_say_calls(calls_for_initial_execution, latest)
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
                        answer, _parsed_tool_calls, chat = parse_agent_output_with_chat(raw_answer)
                    except Exception as exc:
                        answer = provider_failure_answer(exc)
                        chat = None
                        print(f"provider follow-up failed for event {latest['id']}: {exc}", flush=True)
                if answer and not streamed_response:
                    final_say = ensure_expanded_chat_for_say_calls([answer_as_say_call(answer, latest, chat=chat)], latest)
                    tool_results.extend(
                        execute_conversational_tool_calls(config.base_url, final_say)
                    )
                elif streamed_response and not tool_calls:
                    finalize_streamed_response(config.base_url, latest)
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
) -> tuple[str, list[dict], dict | None]:
    raw_answer, native_tool_calls = run_provider_with_retry(client, providers, config, prompt, native_tools)
    answer, parsed_tool_calls, chat = parse_agent_output_with_chat(raw_answer)
    tool_calls = [*native_tool_calls, *parsed_tool_calls]
    if answer and is_echo_answer(answer, pending):
        retry_prompt = build_retry_prompt(prompt, answer)
        raw_answer, native_tool_calls = run_provider_with_retry(client, providers, config, retry_prompt, native_tools)
        answer, parsed_tool_calls, chat = parse_agent_output_with_chat(raw_answer)
        tool_calls = [*native_tool_calls, *parsed_tool_calls]
        if answer and is_echo_answer(answer, pending):
            raise RuntimeError(f"{config.echo_error_label} returned echo response twice: {answer}")
    if answer and not chat and not tool_calls and chat_mode_for_task(pending[-1]) == "expanded_chat":
        chat = generate_expanded_chat_reply(client, providers, config, prompt, pending[-1], answer)
    return answer, tool_calls, chat


def generate_expanded_chat_reply(
    client: Any,
    providers: AgentProviderRegistry,
    config: CommunicationAgentConfig,
    prompt: str,
    task: dict,
    spoken_answer: str,
) -> dict | None:
    caller_text = str(task.get("data", {}).get("text") or "").strip()
    chat_prompt = (
        f"{prompt}\n\n"
        "The previous provider response contained only a short spoken answer, but this voicebot has "
        "expanded chat enabled. Create the written visitor chat answer now.\n\n"
        f"Caller question: {caller_text or '(not provided)'}\n"
        f"Spoken answer already sent to TTS: {spoken_answer}\n\n"
        "Return only JSON in this exact shape:\n"
        '{"chat":{"text":"visitor-readable Markdown explanation that is clearly more detailed than the spoken answer"}}\n\n'
        "Do not include tool_calls. Do not include say. Do not copy the spoken answer as the whole chat text. "
        "Use concise headings or bullets when helpful."
    )
    try:
        raw_chat, _native_tool_calls = run_provider_with_retry(client, providers, config, chat_prompt, None)
    except Exception as exc:
        print(f"provider chat expansion failed for event {task.get('id')}: {exc}", flush=True)
        return None
    _answer, _tool_calls, chat = parse_agent_output_with_chat(raw_chat)
    return chat


def run_model_turn_streaming(
    client: Any,
    providers: AgentProviderRegistry,
    config: CommunicationAgentConfig,
    prompt: str,
    pending: list[dict],
    native_tools: list[dict],
    latest: dict,
) -> tuple[str, list[dict], bool]:
    raw_parts: list[str] = []
    native_tool_calls: list[dict] = []
    pending_text = ""
    streamed = False
    try:
        chunks = providers.run_stream(
            client,
            config.provider,
            config.model,
            prompt,
            config.timeout,
            config.max_output_tokens,
            native_tools,
        )
        for chunk in chunks:
            if chunk.text:
                raw_parts.append(chunk.text)
                pending_text += chunk.text
                ready, pending_text = split_stable_stream_text(pending_text, config.streaming_chunk_chars)
                for text in ready:
                    submit_stream_chunk(config.base_url, latest, text)
                    streamed = True
            native_tool_calls.extend({"name": call.name, "arguments": call.arguments} for call in chunk.tool_calls)
        if pending_text.strip():
            submit_stream_chunk(config.base_url, latest, pending_text.strip())
            streamed = True
    except Exception:
        if streamed:
            finalize_streamed_response(config.base_url, latest)
            return "", [], True
        raise

    raw_answer = "".join(raw_parts)
    answer, parsed_tool_calls = parse_agent_output(raw_answer)
    tool_calls = [*native_tool_calls, *parsed_tool_calls]
    if answer and is_echo_answer(answer, pending):
        retry_prompt = build_retry_prompt(prompt, answer)
        raw_answer, native_tool_calls = run_provider_with_retry(client, providers, config, retry_prompt, native_tools)
        answer, parsed_tool_calls = parse_agent_output(raw_answer)
        tool_calls = [*native_tool_calls, *parsed_tool_calls]
        streamed = False
        if answer and is_echo_answer(answer, pending):
            raise RuntimeError(f"{config.echo_error_label} returned echo response twice: {answer}")
    return ("" if streamed else answer), tool_calls, streamed


def split_stable_stream_text(text: str, chunk_chars: int) -> tuple[list[str], str]:
    ready: list[str] = []
    buffer = text
    while buffer:
        boundary = max(buffer.rfind(". "), buffer.rfind("? "), buffer.rfind("! "), buffer.rfind("\n"))
        if boundary >= 0:
            chunk = buffer[: boundary + 1].strip()
            if chunk:
                ready.append(chunk)
            buffer = buffer[boundary + 1 :].lstrip()
            continue
        if len(buffer) >= chunk_chars:
            split_at = buffer.rfind(" ", 0, chunk_chars)
            if split_at <= 0:
                split_at = chunk_chars
            ready.append(buffer[:split_at].strip())
            buffer = buffer[split_at:].lstrip()
            continue
        break
    return ready, buffer


def submit_stream_chunk(base_url: str, task: dict, text: str) -> None:
    if not text.strip():
        return
    http_json(
        "POST",
        f"{base_url}/calls/{task['call_id']}/responses",
        {
            "text": text.strip(),
            "response_to_event_id": task["id"],
            "response_kind": "stream_chunk",
            "partial": True,
        },
    )


def finalize_streamed_response(base_url: str, task: dict) -> None:
    http_json(
        "POST",
        f"{base_url}/calls/{task['call_id']}/responses",
        {
            "text": "",
            "response_to_event_id": task["id"],
            "response_kind": "stream_finalized",
            "finalize_only": True,
        },
    )


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


def validate_call_control_tool(
    client: Any,
    providers: AgentProviderRegistry,
    config: CommunicationAgentConfig,
    task: dict,
    tool_call: dict,
) -> bool:
    from local_command_agent import call_control_validation_prompt, parse_call_control_validation

    prompt = call_control_validation_prompt(task, tool_call)
    try:
        raw_answer, _tool_calls = run_provider_with_retry(client, providers, config, prompt, None)
    except Exception as exc:
        print(f"call-control validation failed for event {task['id']}: {exc}", flush=True)
        return False
    allowed = parse_call_control_validation(raw_answer)
    if not allowed:
        print(
            f"dropped ungrounded call-control tool {tool_call.get('name')} for event {task['id']}",
            flush=True,
        )
    return allowed


def recover_missing_colleague_tool_call(
    client: Any,
    providers: AgentProviderRegistry,
    config: CommunicationAgentConfig,
    task: dict,
    context: dict,
    draft_answer: str,
    native_tools: list[dict],
) -> list[dict]:
    tool_name = preferred_colleague_tool_name(native_tools)
    if not tool_name or str(task.get("data", {}).get("reason") or "") in {"call_connected", "colleague_result", "colleague_progress"}:
        return []
    prompt = colleague_tool_recovery_prompt(task, context, draft_answer, tool_name)
    try:
        raw_answer, _tool_calls = providers.run(
            client,
            config.provider,
            config.model,
            prompt,
            min(config.timeout, 3.0),
            min(config.max_output_tokens, 180),
            None,
        )
    except Exception as exc:
        print(f"colleague tool recovery failed for event {task['id']}: {exc}", flush=True)
        return deterministic_colleague_tool_recovery(task, context, draft_answer, tool_name)
    recovery = parse_colleague_tool_recovery(raw_answer)
    if not recovery.get("delegate"):
        return deterministic_colleague_tool_recovery(task, context, draft_answer, tool_name)
    message = str(recovery.get("message") or task.get("data", {}).get("text") or "").strip()
    if not message:
        return []
    arguments: dict[str, Any] = {
        "call_id": task["call_id"],
        "message": message,
        "response_to_event_id": task["id"],
    }
    if tool_name == "create_flowhunt_project_issue":
        arguments = {
            "call_id": task["call_id"],
            "title": str(recovery.get("title") or "Caller request").strip()[:120],
            "description": message,
            "response_to_event_id": task["id"],
        }
    return [{"name": tool_name, "arguments": arguments}]


EXTERNAL_WORK_PROMISE_RE = re.compile(
    r"\b(i(?:'|’)ll|i will|let me|i can|i(?:'|’)m going to)\s+"
    r"(check|look up|verify|investigate|research|ask|find out|review|inspect)\b",
    re.IGNORECASE,
)


def deterministic_colleague_tool_recovery(task: dict, context: dict, draft_answer: str, tool_name: str) -> list[dict]:
    if not EXTERNAL_WORK_PROMISE_RE.search(draft_answer):
        return []
    message = colleague_recovery_message_from_context(task, context)
    if not message:
        return []
    arguments: dict[str, Any] = {
        "call_id": task["call_id"],
        "message": message,
        "response_to_event_id": task["id"],
    }
    if tool_name == "create_flowhunt_project_issue":
        arguments = {
            "call_id": task["call_id"],
            "title": "Caller external check",
            "description": message,
            "response_to_event_id": task["id"],
        }
    return [{"name": tool_name, "arguments": arguments}]


def colleague_recovery_message_from_context(task: dict, context: dict) -> str:
    latest_text = str((task.get("data") or {}).get("text") or "").strip()
    recent_texts: list[str] = []
    events = context.get("events")
    if isinstance(events, list):
        for event in events[-20:]:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "")
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            text = str(data.get("text") or "").strip()
            if not text:
                continue
            if event_type in {"user_transcript", "stt_result_dropped"}:
                recent_texts.append(text)
    parts: list[str] = []
    for text in [*recent_texts, latest_text]:
        if text and text.casefold() not in {part.casefold() for part in parts}:
            parts.append(text)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "Use the recent caller context to complete the external check: " + " / ".join(parts[-4:])


def preferred_colleague_tool_name(native_tools: list[dict]) -> str:
    available = {str(tool.get("name") or "") for tool in native_tools if isinstance(tool, dict)}
    if "delegate_to_subagent" in available:
        return "delegate_to_subagent"
    if "invoke_flowhunt_flow" in available:
        return "invoke_flowhunt_flow"
    if "create_flowhunt_project_issue" in available:
        return "create_flowhunt_project_issue"
    return ""


def colleague_tool_recovery_prompt(task: dict, context: dict, draft_answer: str, tool_name: str) -> str:
    recent_events = context.get("events", [])
    if isinstance(recent_events, list):
        recent_events = recent_events[-12:]
    else:
        recent_events = []
    return f"""Decide if the voice agent failed to call a colleague tool.

The voice agent can answer simple conversational questions directly. It must use
{tool_name} when the caller asks for external work, website/status checks,
research, account work, comparisons, or when the draft answer promises future
checking but does not contain a final factual result.

Caller message:
{task.get("data", {}).get("text") or ""}

Draft spoken answer:
{draft_answer}

Conversation summary:
{context.get("summary") or "(none)"}

Recent events:
{json.dumps(recent_events, ensure_ascii=False, indent=2)[:6000]}

Return only JSON:
{{"delegate": true or false, "message": "exact colleague request if delegate is true", "title": "short title"}}

Use the caller's language and the conversation context to reconstruct the exact
request. Do not delegate greetings, thanks, unclear noise, or ordinary small
talk.
"""


def parse_colleague_tool_recovery(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end < start:
            return {}
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return {}
    if not isinstance(data, dict):
        return {}
    return data


def provider_failure_answer(exc: Exception) -> str:
    _ = exc
    return "I had a temporary AI error. Please repeat that once more."


def fallback_answer_for_dropped_tools(task: dict) -> str:
    data = task.get("data", {}) if isinstance(task.get("data"), dict) else {}
    text = str(data.get("text") or "").strip()
    if text:
        separator = "" if text.endswith((".", "!", "?")) else "."
        return f"I heard: {text}{separator} Could you please say the full request again?"
    return "I did not catch the full request. Could you please repeat it?"


def has_http_failed_say(results: list[dict]) -> bool:
    return any(result.get("name") == "say" and not result.get("ok") for result in results)


class DelayedProgressAcknowledgement:
    def __init__(self, base_url: str, task: dict, delay_seconds: float = 2.0) -> None:
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
                    "text": progress_ack_text_for_task(self.task),
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


def progress_ack_text_for_task(task: dict) -> str:
    data = task.get("data", {}) if isinstance(task.get("data"), dict) else {}
    prompt_config = data.get("prompt_config") if isinstance(data.get("prompt_config"), dict) else {}
    configured_filler = str(prompt_config.get("filler_message") or "").strip()
    if configured_filler:
        return configured_filler
    return "Give me a moment."


def colleague_progress_ack_text_for_task(task: dict) -> str:
    data = task.get("data", {}) if isinstance(task.get("data"), dict) else {}
    prompt_config = data.get("prompt_config") if isinstance(data.get("prompt_config"), dict) else {}
    configured_message = str(prompt_config.get("colleague_progress_message") or "").strip()
    if configured_message:
        return configured_message
    return "I asked a colleague to check that. I will tell you the result as soon as it is ready."


def progress_ack_tool_call(task: dict) -> dict:
    return {
        "name": "say",
        "arguments": {
            "call_id": task["call_id"],
            "text": progress_ack_text_for_task(task),
            "response_to_event_id": None,
            "response_kind": "progress_ack",
        },
    }


def colleague_progress_ack_tool_call(task: dict) -> dict:
    return {
        "name": "say",
        "arguments": {
            "call_id": task["call_id"],
            "text": colleague_progress_ack_text_for_task(task),
            "response_to_event_id": None,
            "response_kind": "progress_ack",
        },
    }


def should_prepend_colleague_progress_ack(
    task: dict,
    tool_calls: list[dict],
    *,
    initial_say: dict | None,
    delayed_ack_delivered: bool,
    streamed_response: bool,
) -> bool:
    return (
        should_send_delayed_acknowledgement(task)
        and has_colleague_tool_call(tool_calls)
        and initial_say is None
        and not delayed_ack_delivered
        and not streamed_response
    )


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
