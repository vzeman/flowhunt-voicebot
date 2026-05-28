#!/usr/bin/env python3
"""Anthropic Messages API agent for the voicebot API."""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
from pathlib import Path

try:
    from anthropic import Anthropic
except ModuleNotFoundError:
    Anthropic = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_provider_registry import default_agent_provider_registry
from local_command_agent import (
    attach_response_event_id,
    build_prompt,
    build_retry_prompt,
    ClaimRenewer,
    claim_tasks,
    execute_tool_call,
    fast_tool_call,
    http_json,
    is_echo_answer,
    parse_agent_output,
    release_tasks,
)
from voicebot.providers import normalize_provider, provider_api_key


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Answer pending voicebot tasks with Anthropic.")
    parser.add_argument("--base-url", default=os.getenv("VOICEBOT_AGENT_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--provider", default=os.getenv("VOICEBOT_AGENT_PROVIDER", "anthropic"))
    parser.add_argument("--api-key", default=os.getenv("VOICEBOT_AGENT_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("VOICEBOT_ANTHROPIC_AGENT_MODEL", "claude-sonnet-4-20250514"))
    parser.add_argument("--interval", type=float, default=float(os.getenv("VOICEBOT_ANTHROPIC_AGENT_INTERVAL", "0.5")))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("VOICEBOT_ANTHROPIC_AGENT_TIMEOUT", "30")))
    parser.add_argument("--max-output-tokens", type=int, default=500)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if Anthropic is None:
        raise RuntimeError("The anthropic package is required to run this agent")
    provider = normalize_provider(args.provider)
    if provider != "anthropic":
        raise RuntimeError("anthropic_agent.py only supports VOICEBOT_AGENT_PROVIDER=anthropic")
    api_key = provider_api_key(provider, args.api_key, os.getenv("ANTHROPIC_API_KEY", ""))
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY or VOICEBOT_AGENT_API_KEY is required")

    client = Anthropic(api_key=api_key)
    agent_providers = default_agent_provider_registry()
    owner = f"anthropic-agent:{os.getpid()}"
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

            pending = claim_tasks(args.base_url, pending, owner, max(args.timeout * 2, 30.0))
            if not pending:
                time.sleep(args.interval)
                continue
            claimed_pending = pending

            ttl_seconds = max(args.timeout * 2, 30.0)
            with ClaimRenewer(args.base_url, pending, owner, ttl_seconds):
                latest = pending[-1]
                deterministic_call = fast_tool_call(latest)
                if deterministic_call:
                    execute_tool_call(args.base_url, deterministic_call)
                    seen.add(latest["id"])
                    print(
                        f"executed deterministic tool {deterministic_call['name']} for event {latest['id']}",
                        flush=True,
                    )
                    claimed_pending = []
                    continue

                legacy_tools = http_json("GET", f"{args.base_url}/agent/tools").get("tools", [])
                native_tools = http_json("GET", f"{args.base_url}/agent/tools/schema").get("tools", [])
                prompt = build_prompt(pending, response.get("context", {}), legacy_tools)
                raw_answer, native_tool_calls = agent_providers.run(
                    client,
                    provider,
                    args.model,
                    prompt,
                    args.timeout,
                    args.max_output_tokens,
                    native_tools,
                )
                answer, parsed_tool_calls = parse_agent_output(raw_answer)
                tool_calls = [*native_tool_calls, *parsed_tool_calls]
                if answer and is_echo_answer(answer, pending):
                    retry_prompt = build_retry_prompt(prompt, answer)
                    raw_answer, native_tool_calls = agent_providers.run(
                        client,
                        provider,
                        args.model,
                        retry_prompt,
                        args.timeout,
                        args.max_output_tokens,
                        native_tools,
                    )
                    answer, parsed_tool_calls = parse_agent_output(raw_answer)
                    tool_calls = [*native_tool_calls, *parsed_tool_calls]
                    if answer and is_echo_answer(answer, pending):
                        raise RuntimeError(f"Anthropic agent returned echo response twice: {answer}")

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
        except (OSError, urllib.error.URLError, TimeoutError, RuntimeError) as exc:
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
