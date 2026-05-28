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

`ConversationFlowStore` is the runtime contract for storing definitions. It
requires every saved definition to include `workspace_id`, supports voicebot
specific flows and workspace defaults, and validates state references before a
flow can be used by a live session. Validation checks the initial state and all
transition target states so broken guided flows fail before callers enter them.
It also validates action shape: speak/agent requests need text, subagent tasks
need text and a provider, and transfer actions need a target.

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

`ConversationActionDispatcher` translates these flow actions into existing
runtime events:

- `speak` -> `agent_response_requested` or direct TTS response
- `agent_request` -> communication agent task
- `subagent_task` -> subagent framework task
- `transfer` and `hangup` -> call-control tool events
- `set_data` -> session data update returned to the caller

## Template Data

Action text and prompt templates can use safe `{name}` formatting from:

- call id
- flow id
- current state
- session data
- current event data

Unknown placeholders are left unchanged so an incomplete template does not crash
an active call.

## Session State

`ConversationSessionStateStore` keeps the current flow state per active call:

- call id
- flow id
- current state
- workspace and voicebot scope
- collected data
- event history

The first implementation is in-memory. It defines the contract that durable
storage will implement later so flow execution can survive worker restarts and
scale across multiple runtime workers.

For an existing call id, the store rejects attempts to move state across
workspace, voicebot, or flow boundaries. Durable storage should preserve the
same invariant when sessions are claimed by different workers.
