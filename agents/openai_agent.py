#!/usr/bin/env python3
"""OpenAI Responses API agent for the voicebot API."""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
from pathlib import Path

from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_command_agent import (
    attach_response_event_id,
    build_prompt,
    build_retry_prompt,
    execute_tool_call,
    fast_tool_call,
    http_json,
    is_echo_answer,
    parse_agent_output,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Answer pending voicebot tasks with OpenAI.")
    parser.add_argument("--base-url", default=os.getenv("VOICEBOT_AGENT_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--provider", default=os.getenv("VOICEBOT_AGENT_PROVIDER", "openai-responses"))
    parser.add_argument("--model", default=os.getenv("VOICEBOT_OPENAI_AGENT_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--interval", type=float, default=float(os.getenv("VOICEBOT_OPENAI_AGENT_INTERVAL", "0.5")))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("VOICEBOT_OPENAI_AGENT_TIMEOUT", "30")))
    parser.add_argument("--max-output-tokens", type=int, default=500)
    return parser.parse_args()


def run_openai_agent(
    client: OpenAI,
    provider: str,
    model: str,
    prompt: str,
    timeout: float,
    max_output_tokens: int,
) -> str:
    if provider == "openai-responses":
        return run_responses_agent(client, model, prompt, timeout, max_output_tokens)
    if provider in {
        "openai-chat",
        "openai-chat-compatible",
        "openrouter",
        "groq",
        "together",
        "ollama",
        "lmstudio",
    }:
        return run_chat_agent(client, model, prompt, timeout, max_output_tokens)
    raise RuntimeError(f"unsupported agent provider: {provider}")


def run_responses_agent(client: OpenAI, model: str, prompt: str, timeout: float, max_output_tokens: int) -> str:
    response = client.responses.create(
        model=model,
        input=prompt,
        max_output_tokens=max_output_tokens,
        timeout=timeout,
    )
    return response.output_text.strip()


def run_chat_agent(client: OpenAI, model: str, prompt: str, timeout: float, max_output_tokens: int) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_output_tokens,
        temperature=0,
        timeout=timeout,
    )
    return (response.choices[0].message.content or "").strip()


def main() -> None:
    args = parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required")

    client_kwargs = {"api_key": os.environ["OPENAI_API_KEY"]}
    base_url = os.getenv("VOICEBOT_AGENT_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or ""
    if base_url:
        client_kwargs["base_url"] = base_url
    elif os.environ.get("OPENAI_BASE_URL") == "":
        os.environ.pop("OPENAI_BASE_URL")
    client = OpenAI(**client_kwargs)
    seen: set[int] = set()

    while True:
        try:
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
            raw_answer = run_openai_agent(
                client,
                args.provider,
                args.model,
                prompt,
                args.timeout,
                args.max_output_tokens,
            )
            answer, tool_calls = parse_agent_output(raw_answer)
            if answer and is_echo_answer(answer, pending):
                retry_prompt = build_retry_prompt(prompt, answer)
                raw_answer = run_openai_agent(
                    client,
                    args.provider,
                    args.model,
                    retry_prompt,
                    args.timeout,
                    args.max_output_tokens,
                )
                answer, tool_calls = parse_agent_output(raw_answer)
                if answer and is_echo_answer(answer, pending):
                    raise RuntimeError(f"OpenAI agent returned echo response twice: {answer}")

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
