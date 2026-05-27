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
import json
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


def build_prompt(tasks: list[dict], context: dict) -> str:
    pending_lines = []
    for task in tasks:
        data = task.get("data", {})
        pending_lines.append(
            f"- event_id={task['id']} call_id={task['call_id']} "
            f"user said: {data.get('text', '')}"
        )

    return f"""You are an AI voicebot speaking with a customer on a phone call.
Answer naturally and concisely. Do not mention implementation details, events,
queues, STT, TTS, Asterisk, or SIP. If there are multiple unhandled user
messages, answer them together in one coherent response.

Conversation summary:
{context.get("summary") or "(none)"}

Recent events:
{json.dumps(context.get("events", []), ensure_ascii=False, indent=2)}

Pending user messages:
{chr(10).join(pending_lines)}

Return only the exact text that should be spoken to the customer.
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

            prompt = build_prompt(pending, response.get("context", {}))
            answer = run_agent_command(args.command, prompt)
            latest = pending[-1]
            http_json(
                "POST",
                f"{args.base_url}/calls/{latest['call_id']}/responses",
                {"text": answer, "response_to_event_id": latest["id"]},
            )
            for task in pending:
                seen.add(task["id"])
            print(f"answered {len(pending)} pending event(s) for call {latest['call_id']}")
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            print(f"agent error: {exc}")
            time.sleep(max(args.interval, 2.0))


if __name__ == "__main__":
    main()
