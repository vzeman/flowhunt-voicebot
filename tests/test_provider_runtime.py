from __future__ import annotations

import unittest

from voicebot.events import EventStore
from voicebot.execution_model import ExecutionIds, ExecutionScope
from voicebot.observability import build_timeline, provider_observability_summary
from voicebot.provider_runtime import (
    ProviderCallContext,
    ProviderFailure,
    record_provider_failure,
    record_provider_latency,
)


class ProviderRuntimeTests(unittest.TestCase):
    def context(self) -> ProviderCallContext:
        return ProviderCallContext(
            provider="openai",
            kind="stt",
            model="gpt-4o-transcribe",
            scope=ExecutionScope(
                workspace_id="workspace-1",
                voicebot_id="voicebot-1",
                session_id="session-1",
                call_id="call-1",
            ),
            ids=ExecutionIds(turn_id=3, trace_id="trace-1"),
        )

    def test_record_provider_latency_emits_scoped_metric(self) -> None:
        events = EventStore(max_context_events=20)

        event = record_provider_latency(events, self.context(), 0.42)

        self.assertEqual(event.type, "metrics")
        self.assertEqual(event.data["name"], "stt_provider_latency_seconds")
        self.assertEqual(event.data["value"], 0.42)
        self.assertEqual(event.data["provider"], "openai")
        self.assertEqual(event.data["workspace_id"], "workspace-1")
        self.assertEqual(event.data["turn_id"], 3)
        self.assertEqual(event.data["trace_id"], "trace-1")

    def test_provider_call_context_rejects_invalid_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider is required"):
            ProviderCallContext(provider="", kind="stt", scope=ExecutionScope(call_id="call-1"))
        with self.assertRaisesRegex(ValueError, "unsupported provider call kind"):
            ProviderCallContext(provider="openai", kind="voice", scope=ExecutionScope(call_id="call-1"))
        with self.assertRaisesRegex(ValueError, "model"):
            ProviderCallContext(provider="openai", kind="stt", model=" ", scope=ExecutionScope(call_id="call-1"))

    def test_provider_latency_rejects_negative_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "latency_seconds"):
            record_provider_latency(EventStore(max_context_events=20), self.context(), -0.1)

    def test_record_provider_failure_emits_typed_failure_event(self) -> None:
        events = EventStore(max_context_events=20)

        event = record_provider_failure(
            events,
            self.context(),
            ProviderFailure("rate_limited", "Provider rate limit exceeded", retryable=True, details={"status": 429}),
        )

        self.assertEqual(event.type, "provider_call_failed")
        self.assertEqual(event.data["error_code"], "rate_limited")
        self.assertTrue(event.data["retryable"])
        self.assertEqual(event.data["details"], {"status": 429})
        self.assertEqual(event.data["provider_kind"], "stt")

    def test_provider_failure_rejects_invalid_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "code"):
            ProviderFailure("", "failed")
        with self.assertRaisesRegex(ValueError, "message"):
            ProviderFailure("failed", "")

    def test_provider_observability_counts_typed_provider_failures(self) -> None:
        events = EventStore(max_context_events=20)
        record_provider_latency(events, self.context(), 0.2)
        record_provider_failure(events, self.context(), ProviderFailure("failed", "bad"))

        summary = provider_observability_summary(events.list_events(call_id="call-1"))

        self.assertEqual(summary["providers"]["openai"]["latency_count"], 1)
        self.assertEqual(summary["providers"]["openai"]["failure_count"], 1)

    def test_provider_failure_appears_in_timeline_telemetry(self) -> None:
        events = EventStore(max_context_events=20)
        failure = record_provider_failure(events, self.context(), ProviderFailure("failed", "bad"))

        timeline = build_timeline(events.list_events(call_id="call-1"))

        self.assertEqual(timeline["events"][0]["id"], failure.id)
        self.assertEqual(timeline["events"][0]["category"], "telemetry")


if __name__ == "__main__":
    unittest.main()
