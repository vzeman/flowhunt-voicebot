from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .conversation_flow import ConversationAction, ConversationSessionState
from .events import EventStore, VoicebotEvent
from .execution_model import ExecutionScope


@dataclass(frozen=True)
class ConversationActionDispatchResult:
    action: ConversationAction
    event: VoicebotEvent | None = None
    session_data: dict[str, Any] | None = None


class ConversationActionDispatcher:
    def __init__(self, events: EventStore) -> None:
        self.events = events

    def dispatch(
        self,
        session: ConversationSessionState,
        action: ConversationAction,
        *,
        trigger_event_id: int | None = None,
    ) -> ConversationActionDispatchResult:
        scope = ExecutionScope(
            workspace_id=session.workspace_id or "",
            voicebot_id=session.voicebot_id or "",
            session_id=session.call_id,
            call_id=session.call_id,
        )
        base = {
            "flow_id": session.flow_id,
            "state": session.current_state,
            "trigger_event_id": trigger_event_id,
        }
        if action.type in {"speak", "agent_request"}:
            event = self.events.append_scoped(
                scope,
                "agent_response_requested",
                {
                    **base,
                    "reason": f"conversation_flow_{action.type}",
                    "text": action.text,
                    **action.data,
                },
            )
            return ConversationActionDispatchResult(action, event)
        if action.type == "subagent_task":
            event = self.events.append_scoped(
                scope,
                "agent_response_requested",
                {
                    **base,
                    "reason": "conversation_flow_subagent_task",
                    "text": action.text,
                    "subagent": action.data,
                },
            )
            return ConversationActionDispatchResult(action, event)
        if action.type in {"transfer", "hangup"}:
            event = self.events.append_scoped(
                scope,
                "call_control_requested",
                {
                    **base,
                    "action": action.type,
                    "text": action.text,
                    **action.data,
                },
            )
            return ConversationActionDispatchResult(action, event)
        if action.type == "set_data":
            return ConversationActionDispatchResult(action, None, dict(action.data))
        event = self.events.append_scoped(
            scope,
            "system",
            {**base, "message": f"unsupported conversation action: {action.type}", "action": action.type},
        )
        return ConversationActionDispatchResult(action, event)

    def dispatch_many(
        self,
        session: ConversationSessionState,
        actions: tuple[ConversationAction, ...],
        *,
        trigger_event_id: int | None = None,
    ) -> tuple[ConversationActionDispatchResult, ...]:
        return tuple(self.dispatch(session, action, trigger_event_id=trigger_event_id) for action in actions)
