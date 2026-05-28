#!/usr/bin/env python3
"""OpenAI-compatible entrypoint for the provider-neutral communication agent."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from openai import OpenAI
except ModuleNotFoundError:
    OpenAI = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from communication_agent import CommunicationAgentConfig, run_communication_agent
from voicebot.providers import normalize_provider, provider_api_key, provider_base_url


def env_or_default(name: str, default: str) -> str:
    return os.getenv(name) or default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a provider-neutral voice communication agent with OpenAI SDK.")
    parser.add_argument("--base-url", default=os.getenv("VOICEBOT_AGENT_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--provider", default=os.getenv("VOICEBOT_AGENT_PROVIDER", "openai-responses"))
    parser.add_argument("--provider-base-url", default=os.getenv("VOICEBOT_AGENT_OPENAI_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.getenv("VOICEBOT_AGENT_API_KEY", ""))
    parser.add_argument(
        "--model",
        default=env_or_default(
            "VOICEBOT_COMMUNICATION_AGENT_MODEL",
            env_or_default("VOICEBOT_OPENAI_AGENT_MODEL", "gpt-4.1-mini"),
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(
            env_or_default(
                "VOICEBOT_COMMUNICATION_AGENT_INTERVAL",
                env_or_default("VOICEBOT_OPENAI_AGENT_INTERVAL", "0.5"),
            )
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(
            env_or_default(
                "VOICEBOT_COMMUNICATION_AGENT_TIMEOUT",
                env_or_default("VOICEBOT_OPENAI_AGENT_TIMEOUT", "30"),
            )
        ),
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=int(env_or_default("VOICEBOT_COMMUNICATION_AGENT_MAX_OUTPUT_TOKENS", "220")),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if OpenAI is None:
        raise RuntimeError("The openai package is required to run this entrypoint")

    provider = normalize_provider(args.provider)
    api_key = provider_api_key(provider, args.api_key, os.getenv("OPENAI_API_KEY", ""))
    if not api_key and provider != "ollama":
        raise RuntimeError(f"API key is required when VOICEBOT_AGENT_PROVIDER={provider}")

    client_kwargs = {"api_key": api_key or "ollama"}
    base_url = provider_base_url(provider, args.provider_base_url, os.getenv("OPENAI_BASE_URL", ""))
    if base_url:
        client_kwargs["base_url"] = base_url
    elif os.environ.get("OPENAI_BASE_URL") == "":
        os.environ.pop("OPENAI_BASE_URL")

    run_communication_agent(
        OpenAI(**client_kwargs),
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
