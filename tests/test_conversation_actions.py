from __future__ import annotations

import unittest

from voicebot.conversation_actions import ConversationActionDispatcher
from voicebot.conversation_flow import ConversationAction, ConversationSessionState
from voicebot.events import EventStore


class ConversationActionDispatcherTests(unittest.TestCase):
    def session(self) -> ConversationSessionState:
        return ConversationSessionState(
            call_id="call-1",
            flow_id="flow-1",
            current_state="greeting",
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
        )

    def test_speak_action_requests_agent_response_for_tts_path(self) -> None:
        events = EventStore(max_context_events=20)

        result = ConversationActionDispatcher(events).dispatch(
            self.session(),
            ConversationAction("speak", text="Hello"),
            trigger_event_id=7,
        )

        self.assertEqual(result.event.type, "agent_response_requested")
        self.assertEqual(result.event.data["reason"], "conversation_flow_speak")
        self.assertEqual(result.event.data["text"], "Hello")
        self.assertEqual(result.event.data["workspace_id"], "workspace-1")
        self.assertEqual(result.event.data["voicebot_id"], "voicebot-1")
        self.assertEqual(result.event.data["session_id"], "call-1")
        self.assertEqual(result.event.data["trigger_event_id"], 7)

    def test_agent_request_action_uses_existing_agent_task_event(self) -> None:
        events = EventStore(max_context_events=20)

        result = ConversationActionDispatcher(events).dispatch(
            self.session(),
            ConversationAction("agent_request", text="Answer this caller."),
        )

        self.assertEqual(result.event.type, "agent_response_requested")
        self.assertEqual(result.event.data["reason"], "conversation_flow_agent_request")

    def test_subagent_task_action_marks_subagent_payload(self) -> None:
        events = EventStore(max_context_events=20)

        result = ConversationActionDispatcher(events).dispatch(
            self.session(),
            ConversationAction("subagent_task", text="Check sitemap", data={"provider": "flowhunt_flow"}),
        )

        self.assertEqual(result.event.type, "agent_response_requested")
        self.assertEqual(result.event.data["reason"], "conversation_flow_subagent_task")
        self.assertEqual(result.event.data["subagent"], {"provider": "flowhunt_flow"})

    def test_transfer_and_hangup_actions_emit_call_control_requests(self) -> None:
        events = EventStore(max_context_events=20)
        dispatcher = ConversationActionDispatcher(events)

        transfer = dispatcher.dispatch(self.session(), ConversationAction("transfer", data={"target": "support"}))
        hangup = dispatcher.dispatch(self.session(), ConversationAction("hangup"))

        self.assertEqual(transfer.event.type, "call_control_requested")
        self.assertEqual(transfer.event.data["action"], "transfer")
        self.assertEqual(transfer.event.data["target"], "support")
        self.assertEqual(hangup.event.type, "call_control_requested")
        self.assertEqual(hangup.event.data["action"], "hangup")

    def test_set_data_action_returns_data_without_event(self) -> None:
        events = EventStore(max_context_events=20)

        result = ConversationActionDispatcher(events).dispatch(
            self.session(),
            ConversationAction("set_data", data={"email": "customer@example.com"}),
        )

        self.assertIsNone(result.event)
        self.assertEqual(result.session_data, {"email": "customer@example.com"})
        self.assertEqual(events.list_events(), [])

    def test_dispatch_many_preserves_action_order(self) -> None:
        events = EventStore(max_context_events=20)

        results = ConversationActionDispatcher(events).dispatch_many(
            self.session(),
            (
                ConversationAction("speak", text="One"),
                ConversationAction("hangup"),
            ),
        )

        self.assertEqual([result.event.type for result in results], ["agent_response_requested", "call_control_requested"])


if __name__ == "__main__":
    unittest.main()
