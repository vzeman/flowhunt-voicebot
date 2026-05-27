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
