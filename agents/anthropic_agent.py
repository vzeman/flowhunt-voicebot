#!/usr/bin/env python3
"""Anthropic entrypoint for the provider-neutral communication agent."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from anthropic import Anthropic
except ModuleNotFoundError:
    Anthropic = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from communication_agent import CommunicationAgentConfig, run_communication_agent
from voicebot.providers import normalize_provider, provider_api_key


def env_or_default(name: str, default: str) -> str:
    return os.getenv(name) or default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the provider-neutral voice communication agent with Anthropic.")
    parser.add_argument("--base-url", default=os.getenv("VOICEBOT_AGENT_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--provider", default=os.getenv("VOICEBOT_AGENT_PROVIDER", "anthropic"))
    parser.add_argument("--api-key", default=os.getenv("VOICEBOT_AGENT_API_KEY", ""))
    parser.add_argument(
        "--model",
        default=env_or_default(
            "VOICEBOT_COMMUNICATION_AGENT_MODEL",
            env_or_default("VOICEBOT_ANTHROPIC_AGENT_MODEL", "claude-sonnet-4-20250514"),
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(
            env_or_default(
                "VOICEBOT_COMMUNICATION_AGENT_INTERVAL",
                env_or_default("VOICEBOT_ANTHROPIC_AGENT_INTERVAL", "0.5"),
            )
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(
            env_or_default(
                "VOICEBOT_COMMUNICATION_AGENT_TIMEOUT",
                env_or_default("VOICEBOT_ANTHROPIC_AGENT_TIMEOUT", "30"),
            )
        ),
    )
    parser.add_argument("--max-output-tokens", type=int, default=500)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if Anthropic is None:
        raise RuntimeError("The anthropic package is required to run this entrypoint")

    provider = normalize_provider(args.provider)
    if provider != "anthropic":
        raise RuntimeError("anthropic_agent.py only supports VOICEBOT_AGENT_PROVIDER=anthropic")
    api_key = provider_api_key(provider, args.api_key, os.getenv("ANTHROPIC_API_KEY", ""))
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY or VOICEBOT_AGENT_API_KEY is required")

    run_communication_agent(
        Anthropic(api_key=api_key),
        CommunicationAgentConfig(
            base_url=args.base_url,
            provider=provider,
            model=args.model,
            interval=args.interval,
            timeout=args.timeout,
            max_output_tokens=args.max_output_tokens,
            owner_prefix="communication-agent",
            echo_error_label="communication agent",
        ),
    )


if __name__ == "__main__":
    main()
