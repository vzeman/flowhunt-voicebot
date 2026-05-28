# Conversation Flow Definitions

Voicebots can run in two conversation modes:

- `freeform`: every caller transcript becomes an agent request using a prompt
  template.
- `structured`: the voicebot follows explicit states, transitions, and actions.

The first engine is implemented in `voicebot/conversation_flow.py`. It is
designed to be tested without live audio and then connected to the event stream.

## Flow Definition

A `ConversationFlowDefinition` is workspace-scoped:

- `flow_id`
- `workspace_id`
- `voicebot_id`
- `mode`
- `initial_state`
- `states`
- optional `language`

The FlowHunt workspace remains the tenant boundary. A workspace can own multiple
voicebots, and each voicebot can use a different flow definition.

## States

Each `ConversationStateDefinition` can define:

- `entry_actions`
- `transitions`
- `prompt_template`
- `fallback_action`

Entry actions are useful for greetings, qualification questions, and handoff
messages. Fallback actions are used when no transition matches a caller event.

## Transitions

Transitions currently match:

- event type, for example `user_transcript`, `no_input`, `timeout`, `dtmf`
- optional text snippets in caller transcript
- optional data updates
- optional action before entering the next state

This keeps deterministic guided flows testable without running SIP, WebRTC, STT,
or TTS.

## Actions

Supported action types:

- `speak`
- `agent_request`
- `subagent_task`
- `transfer`
- `hangup`
- `set_data`

The active runtime still uses the existing event-driven agent path. The next
integration step is to translate these flow actions into existing events:

- `speak` -> `agent_response_requested` or direct TTS response
- `agent_request` -> communication agent task
- `subagent_task` -> subagent framework task
- `transfer` and `hangup` -> call-control tool events

## Template Data

Action text and prompt templates can use safe `{name}` formatting from:

- call id
- flow id
- current state
- session data
- current event data

Unknown placeholders are left unchanged so an incomplete template does not crash
an active call.
