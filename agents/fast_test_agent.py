#!/usr/bin/env python3
"""Low-latency test agent for end-to-end phone loop validation."""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Answer pending voicebot tasks with a simple test response.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--interval", type=float, default=0.5)
    return parser.parse_args()


def http_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())


def response_text(task: dict) -> str:
    data = task.get("data", {})
    if data.get("reason") == "call_connected":
        return "Hello, this is the FlowHunt voicebot. How can I help you?"
    text = data.get("text", "").strip()
    if text:
        return f"I heard: {text}"
    return "I heard you, but I did not receive text."


def control_tool(task: dict) -> tuple[str, dict] | None:
    data = task.get("data", {})
    text = data.get("text", "").lower()
    call_id = task["call_id"]
    event_id = task["id"]
    if any(phrase in text for phrase in ("hang up", "hangup", "stop the call", "end the call")):
        return "hangup_call", {"call_id": call_id, "response_to_event_id": event_id}

    match = re.search(r"\btransfer\b.*?\b(?:to|extension)\s+([A-Za-z0-9_.+-]+)", text)
    if match:
        return (
            "transfer_call",
            {
                "call_id": call_id,
                "target": match.group(1),
                "response_to_event_id": event_id,
            },
        )
    return None


def main() -> None:
    args = parse_args()
    seen: set[int] = set()
    while True:
        try:
            tasks = http_json("GET", f"{args.base_url}/agent/tasks").get("pending", [])
            pending = [task for task in tasks if task["id"] not in seen]
            if not pending:
                time.sleep(args.interval)
                continue

            latest = pending[-1]
            tool = control_tool(latest)
            if tool is None:
                tool_name = "say"
                arguments = {
                    "call_id": latest["call_id"],
                    "text": response_text(latest),
                    "response_to_event_id": latest["id"],
                }
            else:
                tool_name, arguments = tool

            http_json("POST", f"{args.base_url}/agent/tools/{tool_name}", {"arguments": arguments})
            for task in pending:
                seen.add(task["id"])
            print(f"answered event {latest['id']} for call {latest['call_id']}", flush=True)
        except Exception as exc:
            print(f"agent error: {exc}", flush=True)
            time.sleep(max(args.interval, 2.0))


if __name__ == "__main__":
    main()
