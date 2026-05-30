from __future__ import annotations

import socket
import threading
import time
import unittest

import numpy as np
from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import AgentResponse, CallRegistry, CallSession, limit_spoken_response_text
from voicebot.config import Settings
from voicebot.events import EventStore
from voicebot.spoken_text import split_spoken_response_text
from voicebot.transcripts import TranscriptStore
from voicebot.webrtc import WebRTCCallSession


class FakeSTT:
    def transcribe(self, call_audio):
        raise AssertionError("not used")

    def transcribe_stream(self, call_audio):
        raise AssertionError("not used")


class FakeTranscriptionResult:
    def __init__(self, text: str, is_final: bool = True) -> None:
        self.text = text
        self.is_final = is_final
        self.reason = None
        self.metadata = {}


class GatedSTT:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def transcribe(self, call_audio, sample_rate=8000):
        _ = call_audio, sample_rate
        self.started.set()
        self.release.wait(timeout=2.0)
        return FakeTranscriptionResult("old question")

    def transcribe_stream(self, call_audio, sample_rate=8000):
        yield self.transcribe(call_audio, sample_rate)


class FakeTTS:
    def synthesize(self, text: str):
        return np.zeros(80, dtype=np.float32), 0.01

    def synthesize_stream(self, text: str):
        yield self.synthesize(text)


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


class RecordingTTS:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def synthesize(self, text: str):
        self.texts.append(text)
        return np.ones(80, dtype=np.float32), 0.01

    def synthesize_stream(self, text: str):
        yield self.synthesize(text)


class PlaybackControlTests(unittest.TestCase):
    def make_session(self, call_id: str, events: EventStore) -> tuple[CallSession, socket.socket, socket.socket]:
        left, right = socket.socketpair()
        session = CallSession(call_id, left, Settings(), events, FakeSTT(), FakeTTS())
        return session, left, right

    def test_call_session_interrupt_playback_clears_buffer_and_records_event(self) -> None:
        events = EventStore(max_context_events=20)
        session, left, right = self.make_session("call-1", events)
        try:
            session.playback.enqueue(np.ones(80, dtype=np.float32))

            event = session.interrupt_playback("test")

            self.assertEqual(event.type, "bot_playback_interrupted")
            self.assertTrue(event.data["interrupted"])
            self.assertFalse(session.playback.is_active())
            self.assertEqual(events.list_events(call_id="call-1"), [event])
        finally:
            left.close()
            right.close()

    def test_stop_playback_tool_records_interrupt_event(self) -> None:
        events = EventStore(max_context_events=20)
        registry = CallRegistry()
        tracker = AgentTaskTracker()
        session, left, right = self.make_session("call-1", events)
        session.playback.enqueue(np.ones(80, dtype=np.float32))
        registry.add(session)
        try:
            app = create_app(
                events,
                registry,
                tracker,
                WebSocketHub(),
                TranscriptStore("/tmp/flowhunt-voicebot-test-transcripts"),
                None,
            )
            client = TestClient(app)

            response = client.post(
                "/agent/tools/stop_playback",
                json={
                    "arguments": {
                        "call_id": "call-1",
                        "reason": "agent_test",
                        "response_to_event_id": 123,
                    }
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn(123, tracker.responded_event_ids)
            event = response.json()["event"]
            self.assertEqual(event["type"], "bot_playback_interrupted")
            self.assertEqual(event["data"]["reason"], "agent_test")
            self.assertTrue(event["data"]["interrupted"])
        finally:
            left.close()
            right.close()

    def test_startup_response_survives_startup_vad_until_recording_clears(self) -> None:
        events = EventStore(max_context_events=20)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"reason": "call_connected"})
            session._remember_response_generation(request.id)
            session._protect_startup_response(request.id)
            session.recording_event.set()
            session._mark_interrupted("startup_noise")

            session.submit_agent_response(
                AgentResponse("call-1", "Hello, how can I help?", response_to_event_id=request.id)
            )
            silent_packet, started, finished, _playback_data = session.next_playback_packet(80)

            self.assertFalse(started)
            self.assertFalse(finished)
            self.assertTrue(session.playback.is_active())
            self.assertEqual(float(np.max(np.abs(silent_packet))), 0.0)
            self.assertNotIn(
                "agent_response_dropped",
                [event.type for event in events.list_events(call_id="call-1")],
            )

            session.recording_event.clear()
            packet, started, _finished, playback_data = session.next_playback_packet(80)

            self.assertTrue(started)
            self.assertEqual(len(packet), 80)
            self.assertEqual(playback_data["response_to_event_id"], request.id)
        finally:
            session.stop()

    def test_colleague_result_waits_for_current_speech_turn(self) -> None:
        events = EventStore(max_context_events=30)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(deferred_response_wait_seconds=1.0),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"reason": "colleague_result"})
            session.recording_event.set()

            def clear_recording() -> None:
                time.sleep(0.05)
                session.recording_event.clear()

            threading.Thread(target=clear_recording, daemon=True).start()

            session.submit_agent_response(
                AgentResponse("call-1", "The answer is 1,950 pages.", response_to_event_id=request.id)
            )

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertIn("agent_response_deferred", event_types)
            self.assertIn("agent_response_queued", event_types)
            self.assertNotIn("agent_response_dropped", event_types)
            self.assertTrue(session.playback.is_active())
        finally:
            session.stop()

    def test_response_waits_for_silence_when_speech_produces_no_new_transcript(self) -> None:
        events = EventStore(max_context_events=30)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(deferred_response_wait_seconds=1.0),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"text": "question"})
            session._remember_response_generation(request.id)
            session.recording_event.set()
            session._mark_interrupted("user_speech_started")

            def clear_recording() -> None:
                time.sleep(0.05)
                session.recording_event.clear()

            threading.Thread(target=clear_recording, daemon=True).start()

            session.submit_agent_response(AgentResponse("call-1", "Here is the answer.", response_to_event_id=request.id))

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertIn("agent_response_deferred", event_types)
            self.assertIn("agent_response_queued", event_types)
            self.assertNotIn("agent_response_dropped", event_types)
            self.assertTrue(session.playback.is_active())
        finally:
            session.stop()

    def test_response_is_dropped_after_newer_transcript(self) -> None:
        events = EventStore(max_context_events=30)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(deferred_response_wait_seconds=1.0),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"text": "old question"})
            session._remember_response_generation(request.id)
            session.recording_event.set()
            events.append("call-1", "user_transcript", {"text": "newer question"})

            def clear_recording() -> None:
                time.sleep(0.05)
                session.recording_event.clear()

            threading.Thread(target=clear_recording, daemon=True).start()

            session.submit_agent_response(AgentResponse("call-1", "Old answer.", response_to_event_id=request.id))

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertIn("agent_response_dropped", event_types)
            self.assertNotIn("agent_response_queued", event_types)
            self.assertFalse(session.playback.is_active())
        finally:
            session.stop()

    def test_response_is_dropped_after_newer_user_speech_even_without_transcript(self) -> None:
        events = EventStore(max_context_events=30)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(deferred_response_wait_seconds=1.0),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"text": "old question"})
            session._remember_response_generation(request.id)
            events.append("call-1", "user_speech_started", {"turn_id": 2})

            session.submit_agent_response(AgentResponse("call-1", "Old answer.", response_to_event_id=request.id))

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertIn("agent_response_dropped", event_types)
            self.assertNotIn("agent_response_queued", event_types)
            self.assertFalse(session.playback.is_active())
        finally:
            session.stop()

    def test_tts_synthesis_latency_metric_is_recorded(self) -> None:
        events = EventStore(max_context_events=30)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"text": "question"})

            session.submit_agent_response(AgentResponse("call-1", "Short answer.", response_to_event_id=request.id))

            metric_names = [
                event.data.get("name")
                for event in events.list_events(call_id="call-1")
                if event.type == "metrics"
            ]
            self.assertIn("tts_synthesis_latency_seconds", metric_names)
            self.assertIn("tts_duration_seconds", metric_names)
        finally:
            session.stop()

    def test_playback_events_include_response_event_id(self) -> None:
        events = EventStore(max_context_events=30)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"text": "question"})
            session.submit_agent_response(AgentResponse("call-1", "Short answer.", response_to_event_id=request.id))

            _packet, started, finished, data = session.next_playback_packet(80)

            self.assertTrue(started)
            self.assertTrue(finished)
            self.assertEqual(data["response_to_event_id"], request.id)
        finally:
            session.stop()

    def test_webrtc_streaming_tts_queues_first_chunk_before_synthesis_finishes(self) -> None:
        events = EventStore(max_context_events=30)
        tts = GatedStreamingTTS()
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(),
            events,
            FakeSTT(),
            tts,
        )
        try:
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
            metric_names = [
                event.data.get("name")
                for event in events.list_events(call_id="call-1")
                if event.type == "metrics"
            ]
            self.assertIn("tts_first_audio_latency_seconds", metric_names)
            self.assertIn("tts_synthesis_latency_seconds", metric_names)
        finally:
            tts.allow_second_chunk.set()
            session.stop()

    def test_webrtc_streaming_tts_drops_remaining_chunks_after_barge_in(self) -> None:
        events = EventStore(max_context_events=40)
        tts = GatedStreamingTTS()
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(greet_on_connect=False, vad_start_ms=0, barge_in_threshold=0.08),
            events,
            FakeSTT(),
            tts,
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"text": "question"})
            session._remember_response_generation(request.id)
            worker = threading.Thread(
                target=session.submit_agent_response,
                args=(AgentResponse("call-1", "Stream this response.", response_to_event_id=request.id),),
                daemon=True,
            )
            worker.start()
            self.assertTrue(tts.first_chunk_ready.wait(timeout=1.0))

            session.process_audio_block(np.full(160, 0.12, dtype=np.float32))
            tts.allow_second_chunk.set()
            worker.join(timeout=1.0)

            self.assertFalse(worker.is_alive())
            self.assertFalse(session.playback.is_active())
            dropped = [event for event in events.list_events(call_id="call-1") if event.type == "agent_response_dropped"]
            self.assertEqual(dropped[-1].data["reason"], "stale_response_after_new_caller_speech")
        finally:
            tts.allow_second_chunk.set()
            session.stop()

    def test_webrtc_stale_stt_result_does_not_request_agent_response(self) -> None:
        events = EventStore(max_context_events=40)
        stt = GatedSTT()
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(greet_on_connect=False),
            events,
            stt,
            FakeTTS(),
        )
        try:
            session._speech_jobs.put((1, np.ones(160, dtype=np.float32), session._current_interrupt_generation()))
            self.assertTrue(stt.started.wait(timeout=1.0))

            session._mark_interrupted("newer_user_speech_started")
            stt.release.set()

            deadline = time.monotonic() + 1.0
            event_types: list[str] = []
            while time.monotonic() < deadline:
                event_types = [event.type for event in events.list_events(call_id="call-1")]
                if "stt_result_dropped" in event_types:
                    break
                time.sleep(0.01)

            self.assertIn("user_transcript", event_types)
            self.assertIn("stt_result_dropped", event_types)
            self.assertNotIn("agent_response_requested", event_types)
            transcript = [event for event in events.list_events(call_id="call-1") if event.type == "user_transcript"][-1]
            self.assertTrue(transcript.data["stale"])
        finally:
            stt.release.set()
            session.stop()

    def test_spoken_response_text_is_limited(self) -> None:
        text = "This is a long first sentence that should stay intact. This second sentence is extra detail."

        self.assertEqual(
            limit_spoken_response_text(text, 55),
            "This is a long first sentence that should stay intact.",
        )

    def test_spoken_response_text_is_split_for_tts(self) -> None:
        text = "First answer sentence. This second answer sentence has enough detail to split by words safely."

        self.assertEqual(
            split_spoken_response_text(text, 45),
            [
                "First answer sentence.",
                "This second answer sentence has enough.",
                "detail to split by words safely.",
            ],
        )

    def test_webrtc_synthesizes_long_response_in_short_spoken_chunks(self) -> None:
        events = EventStore(max_context_events=30)
        tts = RecordingTTS()
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(tts_chunk_chars=50),
            events,
            FakeSTT(),
            tts,
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"text": "question"})

            session.submit_agent_response(
                AgentResponse(
                    "call-1",
                    "Here is the short result. This longer follow up should become another audio request.",
                    response_to_event_id=request.id,
                )
            )

            self.assertEqual(
                tts.texts,
                [
                    "Here is the short result.",
                    "This longer follow up should become another audio.",
                    "request.",
                ],
            )
            self.assertTrue(session.playback.is_active())
        finally:
            session.stop()

    def test_webrtc_barge_in_ignores_audio_below_barge_in_threshold(self) -> None:
        events = EventStore(max_context_events=30)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(
                greet_on_connect=False,
                start_threshold=0.02,
                barge_in_threshold=0.30,
                vad_start_ms=0,
            ),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            session.playback.enqueue(np.ones(1600, dtype=np.float32))

            session.process_audio_block(np.full(160, 0.05, dtype=np.float32))

            self.assertFalse(session.recording_event.is_set())
            self.assertTrue(session.playback.is_active())
            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertNotIn("bot_playback_interrupted", event_types)
        finally:
            session.stop()

    def test_webrtc_barge_in_interrupts_playback_above_barge_in_threshold(self) -> None:
        events = EventStore(max_context_events=30)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(
                greet_on_connect=False,
                start_threshold=0.02,
                barge_in_threshold=0.30,
                vad_start_ms=0,
            ),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            session.playback.enqueue(np.ones(1600, dtype=np.float32))

            session.process_audio_block(np.full(160, 0.5, dtype=np.float32))

            self.assertTrue(session.recording_event.is_set())
            self.assertFalse(session.playback.is_active())
            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertIn("bot_playback_interrupted", event_types)
        finally:
            session.stop()

    def test_webrtc_barge_in_interrupts_during_echo_tail(self) -> None:
        events = EventStore(max_context_events=30)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(
                greet_on_connect=False,
                start_threshold=0.02,
                barge_in_threshold=0.08,
                echo_tail_ms=1000,
                vad_start_ms=0,
            ),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            session.playback.enqueue(np.ones(1600, dtype=np.float32))
            session._set_echo_tail(1000)

            session.process_audio_block(np.full(160, 0.12, dtype=np.float32))

            self.assertTrue(session.recording_event.is_set())
            self.assertFalse(session.playback.is_active())
            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertIn("bot_playback_interrupted", event_types)
        finally:
            session.stop()


if __name__ == "__main__":
    unittest.main()
