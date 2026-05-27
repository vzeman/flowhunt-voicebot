# FlowHunt Voicebot

Prototype SIP voicebot runtime for receiving calls through Asterisk, converting
caller audio to text, sending text events to an external AI agent, and playing
the agent response back into the call.

The first implementation is intentionally modular. The SIP transport, VAD/turn
detection, STT, AI-agent API, TTS, playback, transcripts, and Asterisk control
are separate Python modules so each part can be replaced later.

## Architecture

```text
SIP trunk
  -> Asterisk PJSIP endpoint
  -> Asterisk AudioSocket
  -> voicebot audio session
  -> VAD turn detector
  -> STT provider
  -> event queue / transcript store
  -> external AI agent
  -> TTS provider
  -> interruptible playback to SIP call
```

The design follows the same broad pattern used by modern real-time voice-agent
frameworks such as Pipecat: a transport receives media, processors handle audio
and text frames/events, and service adapters encapsulate STT, agent, and TTS
providers.

## Docker SIP Runtime

Create a local environment file or export variables in your shell. Do not commit
real SIP credentials.

```bash
export SIP_PASSWORD='your-password-here'
export VOICEBOT_WHISPER_MODEL=base
export VOICEBOT_LANGUAGE=en
```

Start the stack:

```bash
docker compose up -d --build
```

Services:

- `asterisk`: registers to the SIP trunk and answers incoming calls.
- `voicebot`: receives Asterisk AudioSocket media, runs STT/TTS, stores events,
  and exposes the agent API on `http://127.0.0.1:8080`.
- `openai-agent`: optional online AI agent using the OpenAI Responses API.

Useful checks:

```bash
docker compose ps
docker compose logs -f asterisk
curl http://127.0.0.1:8080/health
```

## Agent API

The voicebot core does not decide what to say. It emits events and waits for an
external AI agent to answer asynchronously.

Read pending user turns:

```bash
curl http://127.0.0.1:8080/agent/tasks
```

Send an answer:

```bash
curl -X POST http://127.0.0.1:8080/calls/CALL_ID/responses \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello, how can I help you?", "response_to_event_id":123}'
```

Watch events:

```bash
websocat ws://127.0.0.1:8080/ws/events
```

See [AGENTS.md](AGENTS.md) for the full event API, transcripts, context
compaction, local command agent, and call-control endpoints.

## Call Control

The agent can request SIP/Asterisk actions through the API:

- Store and read full call transcripts.
- Observe `call_started`, `user_transcript`, playback, control, DTMF, and
  `call_ended` events.
- Hang up active calls.
- Transfer active calls to another SIP target through Asterisk.

## Local Command Agent

For the first prototype, an external local command can behave as the AI agent:

```bash
python agents/local_command_agent.py \
  --base-url http://127.0.0.1:8080 \
  --command 'codex exec -'
```

The command receives a prompt on stdin and must write only the answer that should
be spoken to the caller.

## OpenAI Provider

The runtime can use OpenAI for the AI agent, STT, and TTS. Put secrets in a
local `.env` file; `.env` is ignored by git.

```bash
SIP_PASSWORD='your-sip-password-here'
OPENAI_API_KEY='your-openai-api-key-here'
VOICEBOT_STT_PROVIDER=openai
VOICEBOT_STT_API_KEY=
VOICEBOT_STT_BASE_URL=
VOICEBOT_STT_MODEL=
VOICEBOT_OPENAI_STT_MODEL=whisper-1
VOICEBOT_TTS_PROVIDER=openai
VOICEBOT_TTS_API_KEY=
VOICEBOT_TTS_BASE_URL=
VOICEBOT_TTS_MODEL=
VOICEBOT_OPENAI_TTS_MODEL=gpt-4o-mini-tts
VOICEBOT_OPENAI_TTS_VOICE=alloy
VOICEBOT_AGENT_PROVIDER=openai-responses
VOICEBOT_AGENT_API_KEY=
VOICEBOT_OPENAI_AGENT_MODEL=gpt-4.1-mini
```

Start the full online-provider stack:

```bash
docker compose up -d --build voicebot asterisk openai-agent
```

Docker Compose lets exported shell variables override `.env` values. If you
already have another `OPENAI_API_KEY` exported in the shell, unset it or start
Compose from a clean shell so the project-local `.env` value is used.

OpenAI model names are configurable so the same runtime can switch back to local
Whisper/Supertonic or use newer OpenAI models without code changes.

Provider names:

- STT: `whisper` for local open-source Whisper, `openai` or
  `openai-compatible` for OpenAI or a compatible transcription endpoint via
  `VOICEBOT_STT_BASE_URL`. Aliases `groq`, `mistral`, `nvidia`, and `xai` use
  the same transcription adapter with provider-specific API key env vars and
  default base URLs.
- TTS: `supertonic` for local Supertonic, `openai` or `openai-compatible` for
  OpenAI or a compatible speech endpoint via `VOICEBOT_TTS_BASE_URL`. Aliases
  `groq`, `mistral`, `nvidia`, and `xai` use the same speech adapter with
  provider-specific API key env vars and default base URLs.
- Agent: `openai-responses` for the OpenAI Responses API, or
  `openai-chat-compatible` for chat-completions providers via
  `VOICEBOT_AGENT_OPENAI_BASE_URL`. Provider aliases `azure`, `cerebras`,
  `deepseek`, `fireworks`, `grok`, `groq`, `mistral`, `nebius`, `novita`,
  `nvidia`, `ollama`, `openrouter`, `perplexity`, `qwen`, `sambanova`,
  `sarvam`, `together`, and `xai` map to the same chat-compatible adapter.

The provider registry also recognizes the broader provider names used by modern
voice-agent stacks. If a provider needs a protocol-specific native adapter, the
runtime fails fast with the exact variables to set for an OpenAI-compatible
gateway until that native adapter is added.

## Local Microphone Echo Demo

The original local microphone/speaker test script is still available:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python listen_transcribe_repeat.py --whisper-model base --language en
```

Use this only for local STT/TTS testing. The SIP voicebot runtime does not repeat
the caller text; it waits for an external agent response.
