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
import os
import re
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll voicebot events and answer through a local AI command.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080", help="Voicebot API base URL.")
    parser.add_argument("--command", required=True, help="Local command to run for response generation.")
    parser.add_argument("--interval", type=float, default=0.5, help="Polling interval in seconds.")
    parser.add_argument("--command-timeout", type=float, default=30.0, help="AI command timeout in seconds.")
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
            {"name": "send_dtmf", "arguments": {"call_id": "...", "digit": "1", "response_to_event_id": 123}},
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
target. If the caller asks you to press or send a keypad digit, call send_dtmf
with one digit. Include response_to_event_id on every tool call.
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


def run_agent_command(command: str, prompt: str, timeout: float) -> str:
    output_file = tempfile.NamedTemporaryFile(prefix="flowhunt-agent-answer-", suffix=".txt", delete=False)
    output_file.close()
    effective_command = add_codex_output_file(command, output_file.name)
    try:
        completed = subprocess.run(
            effective_command,
            input=prompt,
            text=True,
            shell=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        with open(output_file.name, encoding="utf-8") as handle:
            final_answer = handle.read().strip()
        if completed.returncode != 0:
            error = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(error or f"agent command failed with {completed.returncode}")
        if final_answer:
            return final_answer
        stdout = completed.stdout.strip()
        if looks_like_codex_diagnostics(stdout):
            raise RuntimeError("agent command produced diagnostics but no final answer")
        return stdout
    finally:
        try:
            os.unlink(output_file.name)
        except OSError:
            pass


def add_codex_output_file(command: str, path: str) -> str:
    if "codex exec" not in command or "--output-last-message" in command:
        return command
    quoted_path = shlex.quote(path)
    if command.rstrip().endswith(" -"):
        return f"{command.rstrip()[:-2]} --output-last-message {quoted_path} -"
    return f"{command} --output-last-message {quoted_path}"


def looks_like_codex_diagnostics(output: str) -> bool:
    if not output:
        return False
    markers = (
        "stream error:",
        "unexpected status",
        "requires a newer version of codex",
        "mcp client",
        "error:",
    )
    lowered = output.lower()
    return any(marker in lowered for marker in markers)


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


def claim_tasks(base_url: str, tasks: list[dict], owner: str, ttl_seconds: float) -> list[dict]:
    event_ids = [int(task["id"]) for task in tasks]
    if not event_ids:
        return []
    response = http_json(
        "POST",
        f"{base_url}/agent/tasks/claim",
        {"event_ids": event_ids, "owner": owner, "ttl_seconds": ttl_seconds},
    )
    claimed_ids = set(response.get("claimed_event_ids", []))
    return [task for task in tasks if task["id"] in claimed_ids]


def release_tasks(base_url: str, tasks: list[dict], owner: str | None = None) -> dict:
    event_ids = [int(task["id"]) for task in tasks]
    if not event_ids:
        return {"released_event_ids": []}
    payload: dict = {"event_ids": event_ids}
    if owner:
        payload["owner"] = owner
    return http_json("POST", f"{base_url}/agent/tasks/release", payload)


def attach_response_event_id(tool_calls: list[dict], event_id: int) -> list[dict]:
    for call in tool_calls:
        arguments = call.setdefault("arguments", {})
        if isinstance(arguments, dict):
            arguments.setdefault("response_to_event_id", event_id)
    return tool_calls


def fast_tool_call(task: dict) -> dict | None:
    data = task.get("data", {})
    event_id = task["id"]
    call_id = task["call_id"]
    text = str(data.get("text", ""))
    normalized = _normalize(text)

    if data.get("reason") == "call_connected":
        return {
            "name": "say",
            "arguments": {
                "call_id": call_id,
                "text": "Hello, this is the FlowHunt voicebot. How can I help you?",
                "response_to_event_id": event_id,
            },
        }

    if wants_hangup(normalized):
        return {
            "name": "hangup_call",
            "arguments": {"call_id": call_id, "response_to_event_id": event_id},
        }

    transfer_target = requested_transfer_target(text)
    if transfer_target:
        return {
            "name": "transfer_call",
            "arguments": {
                "call_id": call_id,
                "target": transfer_target,
                "response_to_event_id": event_id,
            },
        }

    return None


def wants_hangup(normalized_text: str) -> bool:
    phrases = (
        "hang up",
        "hangup",
        "end the call",
        "stop the call",
        "disconnect",
        "terminate the call",
    )
    return any(phrase in normalized_text for phrase in phrases)


def requested_transfer_target(text: str) -> str | None:
    patterns = (
        r"\btransfer\b.*?\bto\s+(?:extension|number)\s+([A-Za-z0-9_.+-]+)",
        r"\btransfer\b.*?\b(?:extension|number)\s+([A-Za-z0-9_.+-]+)",
        r"\btransfer\b.*?\bto\s+([A-Za-z0-9_.+-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


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
    owner = f"local-command-agent:{os.getpid()}"
    seen: set[int] = set()
    claimed_pending: list[dict] = []

    while True:
        try:
            claimed_pending = []
            active_call_ids = set(http_json("GET", f"{args.base_url}/health").get("active_calls", []))
            response = http_json("GET", f"{args.base_url}/agent/tasks")
            pending = [
                task
                for task in response.get("pending", [])
                if task["id"] not in seen and task.get("call_id") in active_call_ids
            ]
            if not pending:
                time.sleep(args.interval)
                continue

            pending = claim_tasks(args.base_url, pending, owner, max(args.command_timeout * 2, 30.0))
            if not pending:
                time.sleep(args.interval)
                continue
            claimed_pending = pending

            latest = pending[-1]
            deterministic_call = fast_tool_call(latest)
            if deterministic_call:
                execute_tool_call(args.base_url, deterministic_call)
                seen.add(latest["id"])
                print(
                    f"executed deterministic tool {deterministic_call['name']} for event {latest['id']}",
                    flush=True,
                )
                continue

            tools = http_json("GET", f"{args.base_url}/agent/tools").get("tools", [])
            prompt = build_prompt(pending, response.get("context", {}), tools)
            raw_answer = run_agent_command(args.command, prompt, args.command_timeout)
            answer, tool_calls = parse_agent_output(raw_answer)
            if answer and is_echo_answer(answer, pending):
                raw_answer = run_agent_command(args.command, build_retry_prompt(prompt, answer), args.command_timeout)
                answer, tool_calls = parse_agent_output(raw_answer)
                if answer and is_echo_answer(answer, pending):
                    raise RuntimeError(f"agent returned echo response twice: {answer}")
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
            claimed_pending = []
            print(f"answered {len(pending)} pending event(s) for call {latest['call_id']}", flush=True)
        except (OSError, urllib.error.URLError, TimeoutError, RuntimeError, subprocess.SubprocessError) as exc:
            if claimed_pending:
                try:
                    release_tasks(args.base_url, claimed_pending, owner)
                except (OSError, urllib.error.URLError, TimeoutError, RuntimeError):
                    pass
                claimed_pending = []
            print(f"agent error: {exc}", flush=True)
            time.sleep(max(args.interval, 2.0))


if __name__ == "__main__":
    main()
