from __future__ import annotations

import socket
import threading
import time
from types import SimpleNamespace
import unittest
import uuid

import numpy as np

from voicebot.audio import MSG_SLIN8, MSG_TERMINATE, MSG_UUID, float32_to_pcm16_bytes, write_audiosocket_message
from voicebot.calls import AgentResponse, CallSession
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.pipeline_contract import PIPELINE_CONTRACT_VERSION
from voicebot.processor_registry import ProcessorSpec
from voicebot.subagents import SubagentCoordinator, SubagentTask, SubagentTaskRequest, SubagentTaskStore
from voicebot.webrtc import WebRTCCallSession


class FakeSTT:
    def transcribe(self, call_audio):
        raise AssertionError("not used")

    def transcribe_stream(self, call_audio):
        raise AssertionError("not used")


class FakeTTS:
    def synthesize(self, text: str):
        return np.zeros(80, dtype=np.float32), 0.01

    def synthesize_stream(self, text: str):
        yield self.synthesize(text)


class SnapshotPartialSTT:
    def __init__(self) -> None:
        self.partial_calls = 0
        self.final_calls = 0

    def transcribe(self, call_audio, sample_rate=8000):
        self.partial_calls += 1
        return SimpleNamespace(text="partial hello", metadata={"sample_rate": sample_rate}, is_final=True)

    def transcribe_stream(self, call_audio, sample_rate=8000):
        self.final_calls += 1
        yield SimpleNamespace(text="final hello", metadata={"sample_rate": sample_rate}, is_final=True)


class MatchingSpeculativeSTT:
    def transcribe(self, call_audio, sample_rate=8000):
        return SimpleNamespace(
            text="please check website status",
            metadata={"sample_rate": sample_rate},
            is_final=True,
        )

    def transcribe_stream(self, call_audio, sample_rate=8000):
        yield SimpleNamespace(
            text="please check website status",
            metadata={"sample_rate": sample_rate},
            is_final=True,
        )


class ChangedSpeculativeSTT:
    def transcribe(self, call_audio, sample_rate=8000):
        return SimpleNamespace(
            text="please check website status",
            metadata={"sample_rate": sample_rate},
            is_final=True,
        )

    def transcribe_stream(self, call_audio, sample_rate=8000):
        yield SimpleNamespace(
            text="thanks goodbye",
            metadata={"sample_rate": sample_rate},
            is_final=True,
        )


class FakeSubagentProvider:
    kind = "flowhunt_flow"

    def __init__(self) -> None:
        self.requests: list[SubagentTaskRequest] = []

    def submit(self, request: SubagentTaskRequest) -> SubagentTask:
        self.requests.append(request)
        task, _created = SubagentTaskStore().get_or_create_requested(request)
        return task.with_status("running", external_task_id="external-1")

    def poll(self, task: SubagentTask) -> SubagentTask:
        return task

    def cancel(self, task: SubagentTask) -> SubagentTask:
        return task.with_status("cancelled")


class RecordingLifecycle:
    def __init__(self) -> None:
        self.scheduled: list[str] = []

    def schedule(self, task: SubagentTask) -> SubagentTask:
        self.scheduled.append(task.task_id)
        return task


class GatedStreamingTTS:
    def __init__(self) -> None:
        self.first_chunk_ready = threading.Event()
        self.allow_second_chunk = threading.Event()

    def synthesize(self, text: str):
        return np.ones(160, dtype=np.float32), 0.02

    def synthesize_stream(self, text: str):
        yield np.ones(80, dtype=np.float32), 0.01
        self.first_chunk_ready.set()
        self.allow_second_chunk.wait(timeout=2.0)
        yield np.ones(80, dtype=np.float32) * 0.5, 0.01


class CallSessionPipelineTests(unittest.TestCase):
    def test_call_session_uses_registry_default_pipelines(self) -> None:
        left, right = socket.socketpair()
        try:
            session = CallSession(
                "call-1",
                left,
                Settings(),
                EventStore(max_context_events=20),
                FakeSTT(),
                FakeTTS(),
            )

            self.assertEqual([processor.name for processor in session.stt_pipeline.processors], ["stt", "agent-request"])
            self.assertEqual([processor.name for processor in session.tts_pipeline.processors], ["tts"])
        finally:
            left.close()
            right.close()

    def test_call_session_accepts_custom_pipeline_specs(self) -> None:
        left, right = socket.socketpair()
        try:
            session = CallSession(
                "call-1",
                left,
                Settings(),
                EventStore(max_context_events=20),
                FakeSTT(),
                FakeTTS(),
                stt_pipeline_specs=(ProcessorSpec("drop", {"name": "drop-stt"}),),
                tts_pipeline_specs=(ProcessorSpec("passthrough", {"name": "tts-pass"}),),
            )

            self.assertEqual([processor.name for processor in session.stt_pipeline.processors], ["drop-stt"])
            self.assertEqual([processor.name for processor in session.tts_pipeline.processors], ["tts-pass"])
        finally:
            left.close()
            right.close()

    def test_latency_metric_over_budget_emits_violation_event(self) -> None:
        left, right = socket.socketpair()
        events = EventStore(max_context_events=20)
        try:
            session = CallSession(
                "call-1",
                left,
                Settings(latency_budget_agent_seconds=0.1),
                events,
                FakeSTT(),
                FakeTTS(),
            )

            session._record_metric("agent_response_latency_seconds", 0.2, {"event_id": 42})

            persisted = events.list_events(call_id="call-1")
            self.assertEqual([event.type for event in persisted], ["metrics", "latency_budget_exceeded"])
            self.assertEqual(persisted[0].data["budget_seconds"], 0.1)
            self.assertEqual(persisted[1].data["metric_event_id"], persisted[0].id)
        finally:
            left.close()
            right.close()

    def test_session_snapshot_exposes_actor_lanes_and_playback_cancellation(self) -> None:
        left, right = socket.socketpair()
        try:
            session = CallSession(
                "call-1",
                left,
                Settings(),
                EventStore(max_context_events=20),
                FakeSTT(),
                FakeTTS(),
            )
            session.playback.enqueue(np.ones(80, dtype=np.float32))

            session.interrupt_playback("test")

            actors = session.snapshot()["actors"]["lanes"]
            self.assertIn("stt", actors)
            self.assertEqual(actors["tts_playback"]["cancellation_generation"], 1)
            self.assertEqual(actors["tts_playback"]["last_signal"]["reason"], "test")
        finally:
            left.close()
            right.close()

    def test_audiosocket_uuid_lifecycle_events_use_transport_descriptor(self) -> None:
        left, right = socket.socketpair()
        events = EventStore(max_context_events=20)
        call_uuid = uuid.uuid4()
        try:
            session = CallSession(
                "pending",
                left,
                Settings(greet_on_connect=False),
                events,
                FakeSTT(),
                FakeTTS(),
            )
            thread = threading.Thread(target=session.run)
            thread.start()
            write_audiosocket_message(right, MSG_UUID, call_uuid.bytes)
            write_audiosocket_message(right, MSG_TERMINATE)
            thread.join(timeout=1)

            lifecycle = events.list_events(call_id=str(call_uuid))
            self.assertEqual([event.type for event in lifecycle[:2]], ["call_started", "call_connected"])
            self.assertEqual(lifecycle[0].data["transport"], "asterisk_audiosocket")
            self.assertEqual(lifecycle[0].data["sample_rate"], 8000)
            self.assertEqual(lifecycle[0].data["pipeline_version"], PIPELINE_CONTRACT_VERSION)
            self.assertEqual(lifecycle[0].data["external_call_id"], str(call_uuid))
            self.assertEqual(lifecycle[0].data["metadata"], {"audiosocket_uuid": str(call_uuid)})
        finally:
            left.close()
            right.close()

    def test_partial_stt_snapshots_emit_partials_without_duplicate_agent_requests(self) -> None:
        left, right = socket.socketpair()
        events = EventStore(max_context_events=50)
        stt = SnapshotPartialSTT()
        try:
            session = CallSession(
                "pending",
                left,
                Settings(
                    greet_on_connect=False,
                    audiosocket_jitter_buffer_enabled=False,
                    stt_partial_enabled=True,
                    stt_partial_interval_seconds=0.0,
                    stt_partial_min_seconds=0.04,
                    stt_partial_min_chars=4,
                    vad_start_ms=20,
                    silence_ms=40,
                    min_seconds=0.04,
                    turn_coalesce_window_ms=0,
                ),
                events,
                stt,
                FakeTTS(),
            )
            thread = threading.Thread(target=session.run)
            thread.start()
            write_audiosocket_message(right, MSG_UUID, uuid.uuid4().bytes)

            speech = np.ones(160, dtype=np.float32) * 0.2
            silence = np.zeros(160, dtype=np.float32)
            for _ in range(20):
                write_audiosocket_message(right, MSG_SLIN8, float32_to_pcm16_bytes(speech))
            self._wait_for_event(events, "user_transcript_partial")
            for _ in range(3):
                write_audiosocket_message(right, MSG_SLIN8, float32_to_pcm16_bytes(silence))
            self._wait_for_event(events, "agent_response_requested")

            write_audiosocket_message(right, MSG_TERMINATE)
            thread.join(timeout=2)

            persisted = events.list_events()
            partials = [event for event in persisted if event.type == "user_transcript_partial"]
            requests = [event for event in persisted if event.type == "agent_response_requested"]
            self.assertGreaterEqual(len(partials), 1)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].data["text"], "final hello")
            self.assertEqual(partials[0].data["metadata"]["source"], "partial_snapshot")
            self.assertGreaterEqual(stt.partial_calls, 1)
            self.assertEqual(stt.final_calls, 1)
        finally:
            left.close()
            right.close()

    def test_partial_stt_starts_and_confirms_speculative_subagent_task_for_sip(self) -> None:
        left, right = socket.socketpair()
        events = EventStore(max_context_events=100)
        provider = FakeSubagentProvider()
        coordinator = SubagentCoordinator(events=events)
        coordinator.register(provider)
        lifecycle = RecordingLifecycle()
        try:
            session = CallSession(
                "pending",
                left,
                self._speculative_settings(audiosocket_jitter_buffer_enabled=False, streaming_rag_enabled=True),
                events,
                MatchingSpeculativeSTT(),
                FakeTTS(),
                subagent_coordinator=coordinator,
                subagent_lifecycle=lifecycle,
            )
            thread = threading.Thread(target=session.run)
            thread.start()
            call_id = str(uuid.uuid4())
            write_audiosocket_message(right, MSG_UUID, uuid.UUID(call_id).bytes)

            speech = np.ones(160, dtype=np.float32) * 0.2
            silence = np.zeros(160, dtype=np.float32)
            for _ in range(20):
                write_audiosocket_message(right, MSG_SLIN8, float32_to_pcm16_bytes(speech))
            self._wait_for_event(events, "subagent_task_speculative_started")
            for _ in range(3):
                write_audiosocket_message(right, MSG_SLIN8, float32_to_pcm16_bytes(silence))
            self._wait_for_event(events, "subagent_task_speculative_confirmed")

            write_audiosocket_message(right, MSG_TERMINATE)
            thread.join(timeout=2)

            tasks = coordinator.store.list(workspace_id="workspace-1")
            requests = [event for event in events.list_events(call_id=call_id) if event.type == "agent_response_requested"]
            self.assertEqual(len(provider.requests), 1)
            self.assertEqual(len(lifecycle.scheduled), 1)
            self.assertEqual(tasks[0].metadata["speculative_status"], "confirmed")
            self.assertEqual(tasks[0].metadata["final_request_event_id"], requests[-1].id)
            metric_names = {
                event.data["name"]
                for event in events.list_events(call_id=call_id)
                if event.type == "metrics"
            }
            self.assertIn("partial_stt_first_text_seconds", metric_names)
            self.assertIn("partial_stt_to_speculative_start_seconds", metric_names)
            self.assertIn("speech_start_to_speculative_start_seconds", metric_names)
            self.assertIn("speech_finished_to_final_transcript_seconds", metric_names)
            self.assertLess(
                self._event_index(events, "subagent_task_speculative_started"),
                self._event_index(events, "user_speech_finished"),
            )
        finally:
            left.close()
            right.close()

    def test_changed_final_transcript_cancels_speculative_subagent_task(self) -> None:
        left, right = socket.socketpair()
        events = EventStore(max_context_events=100)
        provider = FakeSubagentProvider()
        coordinator = SubagentCoordinator(events=events)
        coordinator.register(provider)
        try:
            session = CallSession(
                "pending",
                left,
                self._speculative_settings(audiosocket_jitter_buffer_enabled=False),
                events,
                ChangedSpeculativeSTT(),
                FakeTTS(),
                subagent_coordinator=coordinator,
                subagent_lifecycle=RecordingLifecycle(),
            )
            thread = threading.Thread(target=session.run)
            thread.start()
            write_audiosocket_message(right, MSG_UUID, uuid.uuid4().bytes)

            speech = np.ones(160, dtype=np.float32) * 0.2
            silence = np.zeros(160, dtype=np.float32)
            for _ in range(5):
                write_audiosocket_message(right, MSG_SLIN8, float32_to_pcm16_bytes(speech))
            self._wait_for_event(events, "subagent_task_speculative_started")
            for _ in range(3):
                write_audiosocket_message(right, MSG_SLIN8, float32_to_pcm16_bytes(silence))
            self._wait_for_event(events, "subagent_task_speculative_cancelled")

            write_audiosocket_message(right, MSG_TERMINATE)
            thread.join(timeout=2)

            task = coordinator.store.list(workspace_id="workspace-1")[0]
            self.assertEqual(task.metadata["speculative_status"], "cancelled")
            self.assertEqual(task.metadata["speculative_cancel_reason"], "final_transcript_changed")
        finally:
            left.close()
            right.close()

    def test_partial_stt_starts_and_confirms_speculative_subagent_task_for_webrtc(self) -> None:
        events = EventStore(max_context_events=100)
        provider = FakeSubagentProvider()
        coordinator = SubagentCoordinator(events=events)
        coordinator.register(provider)
        session = WebRTCCallSession(
            call_id="webrtc-call-1",
            session_id="session-1",
            settings=self._speculative_settings(webrtc_jitter_buffer_enabled=False, streaming_rag_enabled=True),
            event_store=events,
            stt=MatchingSpeculativeSTT(),
            tts=FakeTTS(),
            metadata={"workspace_id": "workspace-1", "voicebot_id": "voicebot-1"},
            subagent_coordinator=coordinator,
            subagent_lifecycle=RecordingLifecycle(),
        )
        self.addCleanup(session.stop)

        speech = np.ones(320, dtype=np.float32) * 0.2
        silence = np.zeros(320, dtype=np.float32)
        for _ in range(20):
            session.process_remote_audio_block(speech)
        self._wait_for_event(events, "subagent_task_speculative_started")
        for _ in range(3):
            session.process_remote_audio_block(silence)
        self._wait_for_event(events, "subagent_task_speculative_confirmed")

        task = coordinator.store.list(workspace_id="workspace-1")[0]
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(task.metadata["speculative_status"], "confirmed")
        self.assertEqual(task.metadata["final_input_text"], "please check website status")
        metric_names = {
            event.data["name"]
            for event in events.list_events(call_id="webrtc-call-1")
            if event.type == "metrics"
        }
        self.assertIn("partial_stt_first_text_seconds", metric_names)
        self.assertIn("partial_stt_to_speculative_start_seconds", metric_names)
        self.assertIn("speech_start_to_speculative_start_seconds", metric_names)
        self.assertIn("speech_finished_to_final_transcript_seconds", metric_names)

    def _wait_for_event(self, events: EventStore, event_type: str, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(event.type == event_type for event in events.list_events()):
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {event_type}")

    def _event_index(self, events: EventStore, event_type: str) -> int:
        for index, event in enumerate(events.list_events()):
            if event.type == event_type:
                return index
        self.fail(f"missing event {event_type}")

    def _speculative_settings(self, **overrides) -> Settings:
        return Settings(
            greet_on_connect=False,
            stt_partial_enabled=True,
            stt_partial_interval_seconds=0.0,
            stt_partial_min_seconds=0.04,
            stt_partial_min_chars=4,
            vad_start_ms=20,
            silence_ms=40,
            min_seconds=0.04,
            turn_coalesce_window_ms=0,
            flowhunt_workspace_id="workspace-1",
            speculative_work_enabled=True,
            speculative_min_chars=8,
            speculative_min_tokens=2,
            **overrides,
        )

    def test_audiosocket_vad_decisions_emit_runtime_metrics(self) -> None:
        left, right = socket.socketpair()
        events = EventStore(max_context_events=20)
        try:
            session = CallSession(
                "call-1",
                left,
                Settings(
                    greet_on_connect=False,
                    start_threshold=0.1,
                    stop_threshold=0.05,
                    vad_start_ms=0,
                    silence_ms=10,
                    min_seconds=999.0,
                    max_seconds=1000.0,
                    packet_ms=10,
                    audiosocket_jitter_target_delay_ms=0,
                ),
                events,
                FakeSTT(),
                FakeTTS(),
            )
            thread = threading.Thread(target=session.run)
            thread.start()
            write_audiosocket_message(right, MSG_SLIN8, float32_to_pcm16_bytes(np.full(80, 0.4, dtype=np.float32)))
            write_audiosocket_message(right, MSG_SLIN8, float32_to_pcm16_bytes(np.zeros(80, dtype=np.float32)))
            write_audiosocket_message(right, MSG_TERMINATE)
            thread.join(timeout=1)

            metrics = [event.data for event in events.list_events(call_id="call-1") if event.type == "metrics"]
            vad_decisions = [metric for metric in metrics if metric["name"] == "vad_decision"]
            self.assertEqual([metric["decision"] for metric in vad_decisions], ["speech_started", "speech_too_short"])
            self.assertEqual(vad_decisions[0]["transport"], "asterisk_audiosocket")
            self.assertIn({"name": "silence_duration_seconds", "value": 0.01, "turn_id": 1}, metrics)
        finally:
            left.close()
            right.close()

    def test_audiosocket_remote_audio_uses_jitter_buffer_before_vad(self) -> None:
        left, right = socket.socketpair()
        events = EventStore(max_context_events=20)
        try:
            session = CallSession(
                "call-1",
                left,
                Settings(
                    greet_on_connect=False,
                    start_threshold=0.1,
                    stop_threshold=0.05,
                    vad_start_ms=0,
                    packet_ms=20,
                    audiosocket_jitter_target_delay_ms=40,
                    audiosocket_jitter_max_delay_ms=80,
                ),
                events,
                FakeSTT(),
                FakeTTS(),
            )
            block = np.full(160, 0.4, dtype=np.float32)

            self.assertEqual(session.process_remote_audio_block(block), 0)
            self.assertEqual(session.process_remote_audio_block(block), 0)
            self.assertEqual(session.process_remote_audio_block(block), 1)

            self.assertEqual(
                [event.type for event in events.list_events(call_id="call-1") if event.type == "user_speech_started"],
                ["user_speech_started"],
            )
            self.assertTrue(session.snapshot()["jitter_buffer"]["enabled"])
        finally:
            left.close()
            right.close()

    def test_audiosocket_remote_audio_can_bypass_jitter_buffer(self) -> None:
        left, right = socket.socketpair()
        events = EventStore(max_context_events=20)
        try:
            session = CallSession(
                "call-1",
                left,
                Settings(
                    greet_on_connect=False,
                    start_threshold=0.1,
                    stop_threshold=0.05,
                    vad_start_ms=0,
                    audiosocket_jitter_buffer_enabled=False,
                ),
                events,
                FakeSTT(),
                FakeTTS(),
            )

            self.assertEqual(session.process_remote_audio_block(np.full(80, 0.4, dtype=np.float32)), 1)

            self.assertFalse(session.snapshot()["jitter_buffer"]["enabled"])
            self.assertEqual(
                [event.type for event in events.list_events(call_id="call-1") if event.type == "user_speech_started"],
                ["user_speech_started"],
            )
        finally:
            left.close()
            right.close()

    def test_audiosocket_barge_in_ignores_audio_below_barge_in_threshold(self) -> None:
        left, right = socket.socketpair()
        events = EventStore(max_context_events=20)
        try:
            session = CallSession(
                "call-1",
                left,
                Settings(
                    greet_on_connect=False,
                    start_threshold=0.02,
                    barge_in_threshold=0.30,
                    vad_start_ms=0,
                    audiosocket_jitter_buffer_enabled=False,
                ),
                events,
                FakeSTT(),
                FakeTTS(),
            )
            session.playback.enqueue(np.ones(800, dtype=np.float32))

            session.process_remote_audio_block(np.full(80, 0.05, dtype=np.float32))

            self.assertFalse(session.recording_event.is_set())
            self.assertTrue(session.playback.is_active())
            self.assertNotIn(
                "bot_playback_interrupted",
                [event.type for event in events.list_events(call_id="call-1")],
            )
        finally:
            left.close()
            right.close()

    def test_audiosocket_barge_in_interrupts_playback_above_barge_in_threshold(self) -> None:
        left, right = socket.socketpair()
        events = EventStore(max_context_events=20)
        try:
            session = CallSession(
                "call-1",
                left,
                Settings(
                    greet_on_connect=False,
                    start_threshold=0.02,
                    barge_in_threshold=0.30,
                    vad_start_ms=0,
                    audiosocket_jitter_buffer_enabled=False,
                ),
                events,
                FakeSTT(),
                FakeTTS(),
            )
            session.playback.enqueue(np.ones(800, dtype=np.float32))

            session.process_remote_audio_block(np.full(80, 0.5, dtype=np.float32))

            self.assertTrue(session.recording_event.is_set())
            self.assertFalse(session.playback.is_active())
            self.assertIn(
                "bot_playback_interrupted",
                [event.type for event in events.list_events(call_id="call-1")],
            )
        finally:
            left.close()
            right.close()

    def test_audiosocket_barge_in_interrupts_during_echo_tail(self) -> None:
        left, right = socket.socketpair()
        events = EventStore(max_context_events=20)
        try:
            session = CallSession(
                "call-1",
                left,
                Settings(
                    greet_on_connect=False,
                    start_threshold=0.02,
                    barge_in_threshold=0.08,
                    echo_tail_ms=1000,
                    vad_start_ms=0,
                    audiosocket_jitter_buffer_enabled=False,
                ),
                events,
                FakeSTT(),
                FakeTTS(),
            )
            session.playback.enqueue(np.ones(800, dtype=np.float32))
            session._set_echo_tail(1000)

            session.process_remote_audio_block(np.full(80, 0.12, dtype=np.float32))

            self.assertTrue(session.recording_event.is_set())
            self.assertFalse(session.playback.is_active())
            self.assertIn(
                "bot_playback_interrupted",
                [event.type for event in events.list_events(call_id="call-1")],
            )
        finally:
            left.close()
            right.close()

    def test_audiosocket_streaming_tts_queues_first_chunk_before_synthesis_finishes(self) -> None:
        left, right = socket.socketpair()
        events = EventStore(max_context_events=30)
        tts = GatedStreamingTTS()
        try:
            session = CallSession("call-1", left, Settings(greet_on_connect=False), events, FakeSTT(), tts)
            request = events.append("call-1", "agent_response_requested", {"text": "question"})
            worker = threading.Thread(
                target=session.submit_agent_response,
                args=(AgentResponse("call-1", "Stream this response.", response_to_event_id=request.id),),
                daemon=True,
            )

            worker.start()
            self.assertTrue(tts.first_chunk_ready.wait(timeout=1.0))

            self.assertTrue(session.playback.is_active())
            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertIn("tts_started", event_types)
            self.assertIn("agent_response_queued", event_types)
            self.assertNotIn("tts_finished", event_types)

            tts.allow_second_chunk.set()
            worker.join(timeout=1.0)
            self.assertFalse(worker.is_alive())
        finally:
            tts.allow_second_chunk.set()
            left.close()
            right.close()

    def test_stream_chunk_response_emits_first_text_and_first_audio_metrics(self) -> None:
        left, right = socket.socketpair()
        events = EventStore(max_context_events=50)
        try:
            session = CallSession("call-1", left, Settings(greet_on_connect=False), events, FakeSTT(), FakeTTS())
            request = events.append("call-1", "agent_response_requested", {"text": "question"})
            with session._response_generation_lock:
                session._response_request_times[request.id] = time.monotonic() - 0.01

            session.submit_agent_response(
                AgentResponse(
                    "call-1",
                    "Stream chunk.",
                    response_to_event_id=request.id,
                    response_kind="stream_chunk",
                    partial=True,
                )
            )

            metrics = {
                event.data["name"]
                for event in events.list_events(call_id="call-1")
                if event.type == "metrics"
            }
            self.assertIn("agent_stream_first_text_latency_seconds", metrics)
            self.assertIn("tts_stream_first_audio_latency_seconds", metrics)
            self.assertIn("response_request_to_first_playback_seconds", metrics)
        finally:
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
