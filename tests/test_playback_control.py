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


class ExplodingSTT:
    def transcribe(self, call_audio, sample_rate=8000):
        raise RuntimeError("stt provider timeout")

    def transcribe_stream(self, call_audio, sample_rate=8000):
        raise RuntimeError("stt provider timeout")


class FakeTranscriptionResult:
    def __init__(self, text: str, is_final: bool = True) -> None:
        self.text = text
        self.is_final = is_final
        self.reason = None
        self.metadata = {}


class GatedSTT:
    def __init__(self, text: str = "old question") -> None:
        self.text = text
        self.started = threading.Event()
        self.release = threading.Event()

    def transcribe(self, call_audio, sample_rate=8000):
        _ = call_audio, sample_rate
        self.started.set()
        self.release.wait(timeout=2.0)
        return FakeTranscriptionResult(self.text)

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

    def test_colleague_result_is_not_lost_after_newer_transcript(self) -> None:
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
            session._remember_response_generation(request.id)
            events.append("call-1", "user_transcript", {"text": "newer caller turn"})

            session.submit_agent_response(
                AgentResponse("call-1", "I checked with a colleague. Here is the result.", response_to_event_id=request.id)
            )

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertNotIn("agent_response_dropped", event_types)
            self.assertIn("agent_response_queued", event_types)
            self.assertTrue(session.playback.is_active())
        finally:
            session.stop()

    def test_call_session_colleague_result_is_not_lost_after_newer_transcript(self) -> None:
        events = EventStore(max_context_events=30)
        session, left, right = self.make_session("call-1", events)
        try:
            request = events.append("call-1", "agent_response_requested", {"reason": "colleague_result"})
            session._remember_response_generation(request.id)
            events.append("call-1", "user_transcript", {"text": "newer caller turn"})

            session.submit_agent_response(
                AgentResponse("call-1", "I checked with a colleague. Here is the result.", response_to_event_id=request.id)
            )

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertNotIn("agent_response_dropped", event_types)
            self.assertIn("agent_response_queued", event_types)
            self.assertTrue(session.playback.is_active())
        finally:
            left.close()
            right.close()

    def test_webrtc_interrupted_colleague_result_resumes_after_empty_followup(self) -> None:
        events = EventStore(max_context_events=40)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(greet_on_connect=False),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"reason": "colleague_result"})
            session._remember_response_generation(request.id)
            session.submit_agent_response(
                AgentResponse("call-1", "I checked with a colleague. Here is the result.", response_to_event_id=request.id)
            )

            _packet, started, _finished, _data = session.next_playback_packet(40)
            self.assertTrue(started)
            session.recording_event.set()
            session.next_playback_packet(40)
            session.recording_event.clear()
            session._maybe_resume_interrupted_persistent_response("stt_no_text")

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertIn("agent_response_resumed", event_types)
            self.assertGreaterEqual(event_types.count("agent_response_queued"), 2)
            self.assertTrue(session.playback.is_active())
        finally:
            session.stop()

    def test_call_session_interrupted_colleague_result_resumes_after_empty_followup(self) -> None:
        events = EventStore(max_context_events=40)
        session, left, right = self.make_session("call-1", events)
        try:
            request = events.append("call-1", "agent_response_requested", {"reason": "colleague_result"})
            session._remember_response_generation(request.id)
            session.submit_agent_response(
                AgentResponse("call-1", "I checked with a colleague. Here is the result.", response_to_event_id=request.id)
            )

            session.playback.next_packet_with_metadata(40)
            session._remember_interrupted_persistent_response("user_speech_started")
            session.playback.interrupt()
            session._maybe_resume_interrupted_persistent_response("stt_no_text")

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertIn("agent_response_resumed", event_types)
            self.assertGreaterEqual(event_types.count("agent_response_queued"), 2)
            self.assertTrue(session.playback.is_active())
        finally:
            left.close()
            right.close()

    def test_call_control_ack_is_not_lost_after_newer_speech(self) -> None:
        events = EventStore(max_context_events=30)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(deferred_response_wait_seconds=0.0),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"text": "please hang up"})
            session._remember_response_generation(request.id)
            events.append("call-1", "user_speech_started", {"reason": "short_noise"})

            session.submit_agent_response(
                AgentResponse(
                    "call-1",
                    "Sure, I will hang up the call now. Goodbye.",
                    response_to_event_id=request.id,
                    response_kind="call_control_ack",
                )
            )

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertNotIn("agent_response_dropped", event_types)
            self.assertIn("agent_response_queued", event_types)
            self.assertTrue(session.playback.is_active())
        finally:
            session.stop()

    def test_call_control_ack_bypasses_active_caller_speech(self) -> None:
        events = EventStore(max_context_events=30)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(deferred_response_wait_seconds=0.0),
            events,
            FakeSTT(),
            FakeTTS(),
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"text": "please hang up"})
            session._remember_response_generation(request.id)
            session.recording_event.set()

            session.submit_agent_response(
                AgentResponse(
                    "call-1",
                    "Goodbye.",
                    response_to_event_id=request.id,
                    response_kind="call_control_ack",
                )
            )

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertNotIn("agent_response_deferred", event_types)
            self.assertNotIn("agent_response_dropped", event_types)
            self.assertIn("agent_response_queued", event_types)
            self.assertTrue(session.playback.is_active())
        finally:
            session.stop()

    def test_call_control_ack_interrupts_generic_progress_ack(self) -> None:
        events = EventStore(max_context_events=30)
        tts = RecordingTTS()
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(),
            events,
            FakeSTT(),
            tts,
        )
        try:
            request = events.append("call-1", "agent_response_requested", {"text": "please hang up"})
            session.submit_agent_response(AgentResponse("call-1", "Give me a moment.", response_kind="progress_ack"))

            session.submit_agent_response(
                AgentResponse(
                    "call-1",
                    "Goodbye.",
                    response_to_event_id=request.id,
                    response_kind="call_control_ack",
                )
            )

            interrupted = [event for event in events.list_events(call_id="call-1") if event.type == "bot_playback_interrupted"]
            self.assertEqual(interrupted[-1].data["reason"], "call_control_ack_priority")
            self.assertEqual(tts.texts, ["Give me a moment.", "Goodbye."])
            self.assertTrue(session.playback.is_active())
            self.assertEqual(session.playback.active_response_kinds(), {"call_control_ack"})
        finally:
            session.stop()

    def test_call_session_call_control_ack_is_not_lost_after_newer_speech(self) -> None:
        events = EventStore(max_context_events=30)
        session, left, right = self.make_session("call-1", events)
        try:
            request = events.append("call-1", "agent_response_requested", {"text": "please hang up"})
            session._remember_response_generation(request.id)
            events.append("call-1", "user_speech_started", {"reason": "short_noise"})

            session.submit_agent_response(
                AgentResponse(
                    "call-1",
                    "Sure, I will hang up the call now. Goodbye.",
                    response_to_event_id=request.id,
                    response_kind="call_control_ack",
                )
            )

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertNotIn("agent_response_dropped", event_types)
            self.assertIn("agent_response_queued", event_types)
            self.assertTrue(session.playback.is_active())
        finally:
            left.close()
            right.close()

    def test_call_session_call_control_ack_bypasses_active_caller_speech(self) -> None:
        events = EventStore(max_context_events=30)
        session, left, right = self.make_session("call-1", events)
        try:
            request = events.append("call-1", "agent_response_requested", {"text": "please hang up"})
            session._remember_response_generation(request.id)
            session.recording_event.set()

            session.submit_agent_response(
                AgentResponse(
                    "call-1",
                    "Goodbye.",
                    response_to_event_id=request.id,
                    response_kind="call_control_ack",
                )
            )

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertNotIn("agent_response_deferred", event_types)
            self.assertNotIn("agent_response_dropped", event_types)
            self.assertIn("agent_response_queued", event_types)
            self.assertTrue(session.playback.is_active())
        finally:
            left.close()
            right.close()

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

    def test_webrtc_stale_stt_result_is_logged_but_not_sent_to_agent(self) -> None:
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
            dropped = None
            while time.monotonic() < deadline:
                drops = [event for event in events.list_events(call_id="call-1") if event.type == "stt_result_dropped"]
                if drops:
                    dropped = drops[-1]
                    break
                time.sleep(0.01)

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertIn("user_transcript", event_types)
            self.assertNotIn("agent_response_requested", event_types)
            self.assertIsNotNone(dropped)
            assert dropped is not None
            self.assertEqual(dropped.data["reason"], "stale_transcript")
            self.assertTrue(dropped.data["stale"])
            self.assertEqual(dropped.data["stale_reason"], "newer_caller_speech_started")
            transcript = [event for event in events.list_events(call_id="call-1") if event.type == "user_transcript"][-1]
            self.assertTrue(transcript.data["stale"])
        finally:
            stt.release.set()
            session.stop()

    def test_webrtc_stale_non_english_transcript_is_dropped(self) -> None:
        events = EventStore(max_context_events=40)
        stt = GatedSTT("Zaveste hovor.")
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
            dropped = None
            while time.monotonic() < deadline:
                drops = [event for event in events.list_events(call_id="call-1") if event.type == "stt_result_dropped"]
                if drops:
                    dropped = drops[-1]
                    break
                time.sleep(0.01)

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertNotIn("agent_response_requested", event_types)
            self.assertIsNotNone(dropped)
            assert dropped is not None
            self.assertEqual(dropped.data["text"], "Zaveste hovor.")
            self.assertEqual(dropped.data["reason"], "stale_transcript")
            self.assertTrue(dropped.data["stale"])
            self.assertEqual(dropped.data["stale_reason"], "newer_caller_speech_started")
        finally:
            stt.release.set()
            session.stop()

    def test_webrtc_low_signal_transcript_is_logged_but_not_sent_to_agent(self) -> None:
        events = EventStore(max_context_events=40)
        stt = GatedSTT("Woof")
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
            stt.release.set()

            deadline = time.monotonic() + 1.0
            dropped = None
            while time.monotonic() < deadline:
                drops = [event for event in events.list_events(call_id="call-1") if event.type == "stt_result_dropped"]
                if drops:
                    dropped = drops[-1]
                    break
                time.sleep(0.01)

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertIn("user_transcript", event_types)
            self.assertNotIn("agent_response_requested", event_types)
            self.assertIsNotNone(dropped)
            assert dropped is not None
            self.assertEqual(dropped.data["reason"], "low_signal_transcript")
            self.assertEqual(dropped.data["text"], "Woof")
        finally:
            stt.release.set()
            session.stop()

    def test_webrtc_short_complete_greeting_is_sent_to_agent(self) -> None:
        events = EventStore(max_context_events=40)
        stt = GatedSTT("Hi.")
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
            stt.release.set()

            deadline = time.monotonic() + 1.0
            requested = None
            while time.monotonic() < deadline:
                requests = [event for event in events.list_events(call_id="call-1") if event.type == "agent_response_requested"]
                if requests:
                    requested = requests[-1]
                    break
                time.sleep(0.01)

            event_types = [event.type for event in events.list_events(call_id="call-1")]
            self.assertIn("user_transcript", event_types)
            self.assertNotIn("stt_result_dropped", event_types)
            self.assertIsNotNone(requested)
            assert requested is not None
            self.assertEqual(requested.data["text"], "Hi.")
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

    def test_short_conversational_response_is_not_split_at_comma(self) -> None:
        text = (
            "I checked with a colleague. The LiveAgent status page currently shows normal operation, "
            "with no active downtime or visible incidents."
        )

        self.assertEqual(split_spoken_response_text(text, 90), [text])

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

    def test_nonpersistent_response_waits_behind_active_colleague_result(self) -> None:
        events = EventStore(max_context_events=30)
        tts = RecordingTTS()
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(),
            events,
            FakeSTT(),
            tts,
        )
        try:
            colleague_request = events.append("call-1", "agent_response_requested", {"reason": "colleague_result"})
            normal_request = events.append("call-1", "agent_response_requested", {"text": "misrecognized waiting turn"})

            session.submit_agent_response(
                AgentResponse(
                    "call-1",
                    "I checked with a colleague. The result is ready.",
                    response_to_event_id=colleague_request.id,
                )
            )
            session.submit_agent_response(
                AgentResponse("call-1", "This unrelated response should not compete.", response_to_event_id=normal_request.id)
            )

            dropped = [event for event in events.list_events(call_id="call-1") if event.type == "agent_response_dropped"]
            self.assertEqual(dropped[-1].data["reason"], "active_colleague_result_playback")
            self.assertEqual(tts.texts, ["I checked with a colleague. The result is ready."])
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

    def test_webrtc_stt_failure_is_recorded_without_silent_worker_loss(self) -> None:
        events = EventStore(max_context_events=40)
        session = WebRTCCallSession(
            "call-1",
            "session-1",
            Settings(greet_on_connect=False),
            events,
            ExplodingSTT(),
            FakeTTS(),
        )
        try:
            session._speech_jobs.put((1, np.ones(160, dtype=np.float32), session._current_interrupt_generation()))
            deadline = time.monotonic() + 1.0
            event_types: list[str] = []
            while time.monotonic() < deadline:
                event_types = [event.type for event in events.list_events(call_id="call-1")]
                if "stt_failed" in event_types:
                    break
                time.sleep(0.01)

            self.assertIn("stt_started", event_types)
            self.assertIn("stt_failed", event_types)
        finally:
            session.stop()

    def test_call_session_stt_failure_is_recorded_without_silent_worker_loss(self) -> None:
        events = EventStore(max_context_events=40)
        left, right = socket.socketpair()
        session = CallSession("call-1", left, Settings(), events, ExplodingSTT(), FakeTTS())
        worker = threading.Thread(target=session._speech_worker_loop, daemon=True)
        try:
            worker.start()
            session._speech_jobs.put((1, np.ones(160, dtype=np.float32), session._current_interrupt_generation()))
            deadline = time.monotonic() + 1.0
            event_types: list[str] = []
            while time.monotonic() < deadline:
                event_types = [event.type for event in events.list_events(call_id="call-1")]
                if "stt_failed" in event_types:
                    break
                time.sleep(0.01)

            self.assertIn("stt_started", event_types)
            self.assertIn("stt_failed", event_types)
        finally:
            session.stop_event.set()
            worker.join(timeout=1.0)
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
