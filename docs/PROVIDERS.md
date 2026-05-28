# Provider Capability Model

Provider selection is based on capabilities, not only provider names. The first
contract lives in `voicebot/providers.py` and is registered by
`voicebot/provider_registry.py`.

## Descriptor

Each provider can expose a `ProviderDescriptor`:

- `provider`: normalized provider name
- `family`: `stt`, `tts`, `agent`, `speech_to_speech`, or `embeddings`
- `adapter`: native adapter, OpenAI-compatible adapter, chat-compatible adapter,
  or declared-only provider
- `capabilities`: modality and runtime behavior
- `models`: known model names when the adapter has stable local defaults
- `config`: provider-specific extension data

## Capabilities

`ProviderCapabilities` currently describes:

- supported modalities, for example `stt`, `streaming_stt`, `tts`, `agent`
- streaming support
- language hints
- required credentials
- latency profile
- interruption support
- output audio format
- usage metadata
- native tool support for agent providers

The `/providers` API includes these capability descriptors so FlowHunt can show
which adapters are available and which capabilities they expose.

## Route-Aware Selection

The registry now has route-aware provider resolution hooks:

- exact `workspace_id + voicebot_id`
- workspace default
- voicebot default
- process default from `Settings`

This is the first step toward selecting STT/TTS/agent providers per FlowHunt
workspace and voicebot. The in-memory route table is intentionally small; it
will be replaced by durable FlowHunt workspace configuration when the admin API
is implemented.

## Current Adapters

Current runtime adapters:

- STT: local Whisper, OpenAI-compatible transcription
- TTS: Supertonic, OpenAI-compatible speech
- Agent: OpenAI Responses, OpenAI-compatible chat, Anthropic

Future adapters should add a descriptor before adding factory code, so pipeline
selection can reason about streaming, latency, credentials, output formats, and
tool support consistently.
