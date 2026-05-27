from __future__ import annotations

import socket
import unittest

import numpy as np

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


if __name__ == "__main__":
    unittest.main()
