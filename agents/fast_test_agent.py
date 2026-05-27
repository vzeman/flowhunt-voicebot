#!/usr/bin/env python3
"""Low-latency test agent for end-to-end phone loop validation."""

from __future__ import annotations

import argparse
import json
import os
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


def claim_tasks(base_url: str, tasks: list[dict], owner: str) -> list[dict]:
    event_ids = [int(task["id"]) for task in tasks]
    if not event_ids:
        return []
    response = http_json(
        "POST",
        f"{base_url}/agent/tasks/claim",
        {"event_ids": event_ids, "owner": owner, "ttl_seconds": 30},
    )
    claimed_ids = set(response.get("claimed_event_ids", []))
    return [task for task in tasks if task["id"] in claimed_ids]


def release_tasks(base_url: str, tasks: list[dict]) -> dict:
    event_ids = [int(task["id"]) for task in tasks]
    if not event_ids:
        return {"released_event_ids": []}
    return http_json("POST", f"{base_url}/agent/tasks/release", {"event_ids": event_ids})


def main() -> None:
    args = parse_args()
    owner = f"fast-test-agent:{os.getpid()}"
    seen: set[int] = set()
    claimed_pending: list[dict] = []
    while True:
        try:
            claimed_pending = []
            tasks = http_json("GET", f"{args.base_url}/agent/tasks").get("pending", [])
            pending = [task for task in tasks if task["id"] not in seen]
            if not pending:
                time.sleep(args.interval)
                continue

            pending = claim_tasks(args.base_url, pending, owner)
            if not pending:
                time.sleep(args.interval)
                continue
            claimed_pending = pending

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
            claimed_pending = []
            print(f"answered event {latest['id']} for call {latest['call_id']}", flush=True)
        except Exception as exc:
            if claimed_pending:
                try:
                    release_tasks(args.base_url, claimed_pending)
                except Exception:
                    pass
                claimed_pending = []
            print(f"agent error: {exc}", flush=True)
            time.sleep(max(args.interval, 2.0))


if __name__ == "__main__":
    main()
