from __future__ import annotations

import unittest

from voicebot.conversation_flow import (
    ConversationAction,
    ConversationFlowDefinition,
    ConversationFlowEngine,
    ConversationFlowStore,
    ConversationSessionState,
    ConversationSessionStateStore,
    ConversationStateDefinition,
    ConversationTransition,
    freeform_flow,
)


class ConversationFlowTests(unittest.TestCase):
    def test_freeform_flow_turns_user_transcript_into_agent_request(self) -> None:
        engine = ConversationFlowEngine(
            freeform_flow(
                flow_id="flow-1",
                workspace_id="workspace-1",
                voicebot_id="voicebot-1",
                prompt_template="Caller said: {text}",
            )
        )
        started = engine.start("call-1")

        result = engine.handle_event(started.session, "user_transcript", {"text": "I need help"})

        self.assertEqual(result.session.workspace_id, "workspace-1")
        self.assertEqual(result.session.voicebot_id, "voicebot-1")
        self.assertEqual(result.actions, (ConversationAction("agent_request", text="Caller said: I need help"),))
        self.assertEqual(result.session.history[0]["event"], "user_transcript")

    def test_structured_flow_runs_entry_actions_on_start(self) -> None:
        definition = ConversationFlowDefinition(
            flow_id="support",
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            mode="structured",
            initial_state="greeting",
            states={
                "greeting": ConversationStateDefinition(
                    "greeting",
                    entry_actions=(ConversationAction("speak", text="Hello, how can I help?"),),
                )
            },
        )

        result = ConversationFlowEngine(definition).start("call-1")

        self.assertEqual(result.session.current_state, "greeting")
        self.assertEqual(result.actions, (ConversationAction("speak", text="Hello, how can I help?"),))

    def test_structured_flow_transitions_and_renders_actions(self) -> None:
        definition = ConversationFlowDefinition(
            flow_id="support",
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            mode="structured",
            initial_state="greeting",
            states={
                "greeting": ConversationStateDefinition(
                    "greeting",
                    transitions=(
                        ConversationTransition(
                            on="user_transcript",
                            to="collect_email",
                            when_text_contains=("account",),
                            action=ConversationAction("agent_request", text="Help with this request: {text}"),
                            data_updates={"topic": "account"},
                        ),
                    ),
                ),
                "collect_email": ConversationStateDefinition(
                    "collect_email",
                    entry_actions=(ConversationAction("speak", text="What email should I check for {topic}?"),),
                ),
            },
        )
        engine = ConversationFlowEngine(definition)
        started = engine.start("call-1")

        result = engine.handle_event(started.session, "user_transcript", {"text": "Account problem"})

        self.assertTrue(result.transitioned)
        self.assertEqual(result.session.current_state, "collect_email")
        self.assertEqual(result.session.data, {"topic": "account"})
        self.assertEqual(
            result.actions,
            (
                ConversationAction("agent_request", text="Help with this request: Account problem"),
                ConversationAction("speak", text="What email should I check for account?"),
            ),
        )

    def test_structured_flow_uses_fallback_without_transition(self) -> None:
        definition = ConversationFlowDefinition(
            flow_id="support",
            workspace_id=None,
            voicebot_id=None,
            mode="structured",
            initial_state="greeting",
            states={
                "greeting": ConversationStateDefinition(
                    "greeting",
                    fallback_action=ConversationAction("speak", text="Please rephrase that."),
                )
            },
        )
        engine = ConversationFlowEngine(definition)
        started = engine.start("call-1")

        result = engine.handle_event(started.session, "user_transcript", {"text": "unknown"})

        self.assertFalse(result.transitioned)
        self.assertEqual(result.actions, (ConversationAction("speak", text="Please rephrase that."),))

    def test_unknown_state_reference_fails_early(self) -> None:
        definition = ConversationFlowDefinition(
            flow_id="broken",
            workspace_id=None,
            voicebot_id=None,
            mode="structured",
            initial_state="missing",
            states={},
        )

        with self.assertRaisesRegex(ValueError, "unknown state 'missing'"):
            ConversationFlowEngine(definition)

    def test_flow_identity_validation_fails_early(self) -> None:
        invalid_definitions = [
            (
                ConversationFlowDefinition(
                    flow_id="",
                    workspace_id="workspace-1",
                    voicebot_id="voicebot-1",
                    states={"default": ConversationStateDefinition("default")},
                ),
                "flow_id",
            ),
            (
                ConversationFlowDefinition(
                    flow_id="flow-1",
                    workspace_id="workspace-1",
                    voicebot_id="voicebot-1",
                    initial_state="",
                    states={"default": ConversationStateDefinition("default")},
                ),
                "initial_state",
            ),
            (
                ConversationFlowDefinition(
                    flow_id="flow-1",
                    workspace_id="workspace-1",
                    voicebot_id="voicebot-1",
                    initial_state="default",
                    states={"": ConversationStateDefinition("")},
                ),
                "state id",
            ),
            (
                ConversationFlowDefinition(
                    flow_id="flow-1",
                    workspace_id="workspace-1",
                    voicebot_id="voicebot-1",
                    initial_state="default",
                    states={"default": ConversationStateDefinition("other")},
                ),
                "state key",
            ),
        ]

        for definition, message in invalid_definitions:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    ConversationFlowStore().save(definition)

    def test_unknown_transition_target_fails_early(self) -> None:
        definition = ConversationFlowDefinition(
            flow_id="broken-transition",
            workspace_id="workspace-1",
            voicebot_id="voicebot-1",
            mode="structured",
            initial_state="start",
            states={
                "start": ConversationStateDefinition(
                    "start",
                    transitions=(ConversationTransition(on="user_transcript", to="missing"),),
                )
            },
        )

        with self.assertRaisesRegex(ValueError, "unknown state 'missing'"):
            ConversationFlowEngine(definition)
        with self.assertRaisesRegex(ValueError, "unknown state 'missing'"):
            ConversationFlowStore().save(definition)

    def test_malformed_actions_fail_early(self) -> None:
        malformed_definitions = [
            ConversationFlowDefinition(
                flow_id="missing-speak",
                workspace_id="workspace-1",
                voicebot_id="voicebot-1",
                mode="structured",
                initial_state="start",
                states={"start": ConversationStateDefinition("start", entry_actions=(ConversationAction("speak"),))},
            ),
            ConversationFlowDefinition(
                flow_id="missing-transfer-target",
                workspace_id="workspace-1",
                voicebot_id="voicebot-1",
                mode="structured",
                initial_state="start",
                states={"start": ConversationStateDefinition("start", entry_actions=(ConversationAction("transfer"),))},
            ),
            ConversationFlowDefinition(
                flow_id="missing-subagent-provider",
                workspace_id="workspace-1",
                voicebot_id="voicebot-1",
                mode="structured",
                initial_state="start",
                states={
                    "start": ConversationStateDefinition(
                        "start",
                        transitions=(
                            ConversationTransition(
                                on="user_transcript",
                                action=ConversationAction("subagent_task", text="Check this"),
                            ),
                        ),
                    )
                },
            ),
        ]

        for definition in malformed_definitions:
            with self.subTest(flow_id=definition.flow_id):
                with self.assertRaisesRegex(ValueError, "conversation action"):
                    ConversationFlowStore().save(definition)

    def test_flow_store_requires_workspace_scoped_definitions(self) -> None:
        store = ConversationFlowStore()

        with self.assertRaisesRegex(ValueError, "workspace-scoped"):
            store.save(freeform_flow(workspace_id=None, voicebot_id="voicebot-1"))

    def test_flow_store_lists_and_resolves_voicebot_default(self) -> None:
        store = ConversationFlowStore()
        workspace_default = store.save(freeform_flow(flow_id="workspace", workspace_id="workspace-1"))
        voicebot_flow = store.save(
            freeform_flow(flow_id="voicebot", workspace_id="workspace-1", voicebot_id="voicebot-1")
        )

        self.assertEqual(store.get("workspace-1", "voicebot-1", "voicebot"), voicebot_flow)
        self.assertEqual(store.default_for_voicebot("workspace-1", "voicebot-1"), voicebot_flow)
        self.assertEqual(store.default_for_voicebot("workspace-1", "voicebot-2"), workspace_default)
        self.assertEqual([flow.flow_id for flow in store.list("workspace-1", "voicebot-1")], ["voicebot"])

    def test_session_state_store_keeps_conversation_state_per_call(self) -> None:
        engine = ConversationFlowEngine(freeform_flow(workspace_id="workspace-1", voicebot_id="voicebot-1"))
        store = ConversationSessionStateStore()
        started = engine.start("call-1")
        handled = engine.handle_event(started.session, "user_transcript", {"text": "hello"})

        store.save(handled.session)

        self.assertEqual(store.get("call-1"), handled.session)
        self.assertEqual(store.list("workspace-1", "voicebot-1"), (handled.session,))
        self.assertTrue(store.delete("call-1"))
        self.assertIsNone(store.get("call-1"))

    def test_session_state_store_rejects_scope_moves_for_existing_call(self) -> None:
        store = ConversationSessionStateStore()
        store.save(ConversationSessionState("call-1", "flow-1", "start", "workspace-1", "voicebot-1"))

        invalid_sessions = [
            ConversationSessionState("call-1", "flow-1", "start", "workspace-2", "voicebot-1"),
            ConversationSessionState("call-1", "flow-1", "start", "workspace-1", "voicebot-2"),
            ConversationSessionState("call-1", "flow-2", "start", "workspace-1", "voicebot-1"),
        ]

        for session in invalid_sessions:
            with self.subTest(session=session):
                with self.assertRaisesRegex(ValueError, "cannot move conversation session"):
                    store.save(session)


if __name__ == "__main__":
    unittest.main()
