from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal


ConversationMode = Literal["freeform", "structured"]
ConversationEventType = Literal[
    "call_connected",
    "user_transcript",
    "no_input",
    "timeout",
    "agent_result",
    "dtmf",
    "error",
]
ConversationActionType = Literal[
    "speak",
    "agent_request",
    "subagent_task",
    "transfer",
    "hangup",
    "set_data",
]


@dataclass(frozen=True)
class ConversationAction:
    type: ConversationActionType
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationTransition:
    on: ConversationEventType
    to: str | None = None
    action: ConversationAction | None = None
    when_text_contains: tuple[str, ...] = ()
    data_updates: dict[str, Any] = field(default_factory=dict)

    def matches(self, event_type: ConversationEventType, event_data: dict[str, Any]) -> bool:
        if self.on != event_type:
            return False
        if not self.when_text_contains:
            return True
        text = str(event_data.get("text", "")).lower()
        return any(candidate.lower() in text for candidate in self.when_text_contains)


@dataclass(frozen=True)
class ConversationStateDefinition:
    state_id: str
    entry_actions: tuple[ConversationAction, ...] = ()
    transitions: tuple[ConversationTransition, ...] = ()
    prompt_template: str = "{text}"
    fallback_action: ConversationAction | None = None


@dataclass(frozen=True)
class ConversationFlowDefinition:
    flow_id: str
    workspace_id: str | None
    voicebot_id: str | None
    mode: ConversationMode = "freeform"
    initial_state: str = "default"
    states: dict[str, ConversationStateDefinition] = field(default_factory=dict)
    language: str | None = None

    def state(self, state_id: str) -> ConversationStateDefinition:
        try:
            return self.states[state_id]
        except KeyError as exc:
            raise ValueError(f"Conversation flow '{self.flow_id}' references unknown state '{state_id}'") from exc


@dataclass(frozen=True)
class ConversationSessionState:
    call_id: str
    flow_id: str
    current_state: str
    workspace_id: str | None = None
    voicebot_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    history: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ConversationStepResult:
    session: ConversationSessionState
    actions: tuple[ConversationAction, ...]
    transitioned: bool = False


class ConversationFlowEngine:
    def __init__(self, definition: ConversationFlowDefinition) -> None:
        self.definition = definition
        self.definition.state(self.definition.initial_state)

    def start(self, call_id: str) -> ConversationStepResult:
        session = ConversationSessionState(
            call_id=call_id,
            flow_id=self.definition.flow_id,
            current_state=self.definition.initial_state,
            workspace_id=self.definition.workspace_id,
            voicebot_id=self.definition.voicebot_id,
        )
        return ConversationStepResult(
            session=session,
            actions=self._render_actions(self.definition.state(session.current_state).entry_actions, session, {}),
        )

    def handle_event(
        self,
        session: ConversationSessionState,
        event_type: ConversationEventType,
        event_data: dict[str, Any] | None = None,
    ) -> ConversationStepResult:
        data = dict(event_data or {})
        if self.definition.mode == "freeform":
            return self._handle_freeform(session, event_type, data)

        state = self.definition.state(session.current_state)
        for transition in state.transitions:
            if not transition.matches(event_type, data):
                continue
            next_state = transition.to or session.current_state
            next_session = self._advance_session(session, event_type, data, next_state, transition.data_updates)
            actions: list[ConversationAction] = []
            if transition.action is not None:
                actions.append(self._render_action(transition.action, next_session, data))
            if next_state != session.current_state:
                actions.extend(self._render_actions(self.definition.state(next_state).entry_actions, next_session, data))
            return ConversationStepResult(next_session, tuple(actions), transitioned=next_state != session.current_state)

        fallback = state.fallback_action
        next_session = self._advance_session(session, event_type, data, session.current_state, {})
        if fallback is None:
            return ConversationStepResult(next_session, ())
        return ConversationStepResult(next_session, (self._render_action(fallback, next_session, data),))

    def _handle_freeform(
        self,
        session: ConversationSessionState,
        event_type: ConversationEventType,
        event_data: dict[str, Any],
    ) -> ConversationStepResult:
        next_session = self._advance_session(session, event_type, event_data, session.current_state, {})
        if event_type != "user_transcript":
            return ConversationStepResult(next_session, ())
        state = self.definition.state(session.current_state)
        text = render_template(state.prompt_template, next_session, event_data)
        return ConversationStepResult(next_session, (ConversationAction("agent_request", text=text),))

    def _advance_session(
        self,
        session: ConversationSessionState,
        event_type: ConversationEventType,
        event_data: dict[str, Any],
        next_state: str,
        data_updates: dict[str, Any],
    ) -> ConversationSessionState:
        self.definition.state(next_state)
        merged_data = {**session.data, **data_updates}
        history_item = {
            "event": event_type,
            "state": session.current_state,
            "next_state": next_state,
            "data": event_data,
        }
        return replace(
            session,
            current_state=next_state,
            data=merged_data,
            history=(*session.history, history_item),
        )

    def _render_actions(
        self,
        actions: tuple[ConversationAction, ...],
        session: ConversationSessionState,
        event_data: dict[str, Any],
    ) -> tuple[ConversationAction, ...]:
        return tuple(self._render_action(action, session, event_data) for action in actions)

    def _render_action(
        self,
        action: ConversationAction,
        session: ConversationSessionState,
        event_data: dict[str, Any],
    ) -> ConversationAction:
        return replace(action, text=render_template(action.text, session, event_data))


def freeform_flow(
    *,
    flow_id: str = "freeform",
    workspace_id: str | None = None,
    voicebot_id: str | None = None,
    prompt_template: str = "{text}",
) -> ConversationFlowDefinition:
    return ConversationFlowDefinition(
        flow_id=flow_id,
        workspace_id=workspace_id,
        voicebot_id=voicebot_id,
        mode="freeform",
        initial_state="default",
        states={"default": ConversationStateDefinition("default", prompt_template=prompt_template)},
    )


def render_template(template: str, session: ConversationSessionState, event_data: dict[str, Any]) -> str:
    values = {
        "call_id": session.call_id,
        "flow_id": session.flow_id,
        "state": session.current_state,
        **session.data,
        **event_data,
    }
    return template.format_map(_SafeFormat(values))


class _SafeFormat(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
