from __future__ import annotations

import socket
import threading
import unittest
import uuid

import numpy as np

from voicebot.audio import MSG_SLIN8, MSG_TERMINATE, MSG_UUID, float32_to_pcm16_bytes, write_audiosocket_message
from voicebot.calls import CallSession
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.processor_registry import ProcessorSpec


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
            self.assertEqual(lifecycle[0].data["external_call_id"], str(call_uuid))
            self.assertEqual(lifecycle[0].data["metadata"], {"audiosocket_uuid": str(call_uuid)})
        finally:
            left.close()
            right.close()

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


if __name__ == "__main__":
    unittest.main()
