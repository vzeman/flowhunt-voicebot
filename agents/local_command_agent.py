#!/usr/bin/env python3
"""
External AI-agent bridge for the voicebot API.

The voicebot service is intentionally agent-agnostic. This script polls pending
voicebot tasks, sends the event context to a local command over stdin, then posts
the command output back as the spoken answer.

Example:

    VOICEBOT_AGENT_COMMAND='codex exec -' python agents/local_command_agent.py

Use any command that accepts a prompt on stdin and writes the final answer to
stdout.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import time
import urllib.error
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll voicebot events and answer through a local AI command.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080", help="Voicebot API base URL.")
    parser.add_argument("--command", required=True, help="Local command to run for response generation.")
    parser.add_argument("--interval", type=float, default=0.5, help="Polling interval in seconds.")
    return parser.parse_args()


def http_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def build_prompt(tasks: list[dict], context: dict, tools: list[dict]) -> str:
    pending_lines = []
    for task in tasks:
        data = task.get("data", {})
        reason = data.get("reason", "user_transcript")
        label = "instruction" if reason == "call_connected" else "user said"
        pending_lines.append(
            f"- event_id={task['id']} call_id={task['call_id']} "
            f"reason={reason} {label}: {data.get('text', '')}"
        )
    output_format = {
        "say": "text to speak, if any",
        "tool_calls": [
            {"name": "hangup_call", "arguments": {"call_id": "...", "response_to_event_id": 123}},
            {"name": "transfer_call", "arguments": {"call_id": "...", "target": "123", "response_to_event_id": 123}},
        ],
    }

    return f"""You are an AI voicebot speaking with a customer on a phone call.
Your job is to help the caller, answer their questions, solve practical
problems, and use tools when a phone action is needed.

Do not repeat the caller's words back as the whole answer. Treat transcripts as
requests, not dictation. Answer naturally and concisely in one or two spoken
sentences unless the caller asks for detail. Do not mention implementation
details, events, queues, STT, TTS, Asterisk, or SIP. If there are multiple
unhandled user messages, answer them together in one coherent response.
If the caller asks to end the call, call the hangup_call tool. If the caller
asks to transfer the call, call transfer_call with the requested extension or
target. Include response_to_event_id on every tool call.
If the caller asks something you can inspect on this computer, use your local
shell/tooling to find the answer before responding. If you cannot complete a
request, say what is missing and ask one short follow-up question.
For website questions, try to inspect the URL with available local networking
tools before answering. Never answer by saying only that you heard the request.

Conversation summary:
{context.get("summary") or "(none)"}

Recent events:
{json.dumps(context.get("events", []), ensure_ascii=False, indent=2)}

Pending user messages:
{chr(10).join(pending_lines)}

Available tools:
{json.dumps(tools, ensure_ascii=False, indent=2)}

Return either plain text to speak, or JSON in this form:
{json.dumps(output_format, ensure_ascii=False, indent=2)}
"""


def build_retry_prompt(original_prompt: str, bad_answer: str) -> str:
    return f"""{original_prompt}

Your previous answer was rejected because it repeated the caller instead of
helping them:
{bad_answer}

Produce a useful answer now. Do not use phrases like "I heard" or restate the
caller's request as the whole response.
"""


def run_agent_command(command: str, prompt: str) -> str:
    completed = subprocess.run(
        command,
        input=prompt,
        text=True,
        shell=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"agent command failed with {completed.returncode}")
    return completed.stdout.strip()


def parse_agent_output(output: str) -> tuple[str, list[dict]]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return output, []
    if not isinstance(data, dict):
        return output, []
    tool_calls = data.get("tool_calls", [])
    if not isinstance(tool_calls, list):
        tool_calls = []
    say = data.get("say") or data.get("text") or ""
    return str(say).strip(), [call for call in tool_calls if isinstance(call, dict)]


def execute_tool_call(base_url: str, call: dict) -> dict:
    name = call.get("name")
    arguments = call.get("arguments") or {}
    if not name:
        raise RuntimeError("tool call missing name")
    if not isinstance(arguments, dict):
        raise RuntimeError(f"tool call {name} arguments must be an object")
    return http_json("POST", f"{base_url}/agent/tools/{name}", {"arguments": arguments})


def attach_response_event_id(tool_calls: list[dict], event_id: int) -> list[dict]:
    for call in tool_calls:
        arguments = call.setdefault("arguments", {})
        if isinstance(arguments, dict):
            arguments.setdefault("response_to_event_id", event_id)
    return tool_calls


def is_echo_answer(answer: str, pending: list[dict]) -> bool:
    normalized_answer = _normalize(answer)
    if not normalized_answer:
        return False
    if normalized_answer.startswith(("i heard ", "you said ", "i heard you say ")):
        return True

    latest_text = _normalize(str(pending[-1].get("data", {}).get("text", "")))
    if not latest_text:
        return False
    if normalized_answer in {latest_text, f"i heard {latest_text}", f"you said {latest_text}"}:
        return True
    return difflib.SequenceMatcher(None, normalized_answer, latest_text).ratio() >= 0.88


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def main() -> None:
    args = parse_args()
    seen: set[int] = set()

    while True:
        try:
            response = http_json("GET", f"{args.base_url}/agent/tasks")
            pending = [task for task in response.get("pending", []) if task["id"] not in seen]
            if not pending:
                time.sleep(args.interval)
                continue

            tools = http_json("GET", f"{args.base_url}/agent/tools").get("tools", [])
            prompt = build_prompt(pending, response.get("context", {}), tools)
            raw_answer = run_agent_command(args.command, prompt)
            answer, tool_calls = parse_agent_output(raw_answer)
            if answer and is_echo_answer(answer, pending):
                raw_answer = run_agent_command(args.command, build_retry_prompt(prompt, answer))
                answer, tool_calls = parse_agent_output(raw_answer)
                if answer and is_echo_answer(answer, pending):
                    raise RuntimeError(f"agent returned echo response twice: {answer}")
            latest = pending[-1]
            tool_calls = attach_response_event_id(tool_calls, latest["id"])
            for call in tool_calls:
                execute_tool_call(args.base_url, call)
            if answer:
                execute_tool_call(
                    args.base_url,
                    {
                        "name": "say",
                        "arguments": {
                            "call_id": latest["call_id"],
                            "text": answer,
                            "response_to_event_id": latest["id"],
                        },
                    },
                )
            for task in pending:
                seen.add(task["id"])
            print(f"answered {len(pending)} pending event(s) for call {latest['call_id']}", flush=True)
        except (OSError, urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            print(f"agent error: {exc}", flush=True)
            time.sleep(max(args.interval, 2.0))


if __name__ == "__main__":
    main()
