from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import unittest

import numpy as np

from voicebot.core_processors import AgentRequestProcessor, EventLogProcessor, STTProcessor, TTSProcessor
from voicebot.events import EventStore
from voicebot.fanout import FanOutProcessor
from voicebot.frames import AudioInputFrame, AudioOutputFrame, Frame, MetricsFrame, TextFrame, TranscriptionFrame
from voicebot.pipeline import PipelineContext, PipelineRunner
from voicebot.processor_registry import ProcessorDependencies, ProcessorSpec, default_processor_registry
from voicebot.processors import DropProcessor, FrameProcessorBase, PassthroughProcessor


class ExplodingProcessor(FrameProcessorBase):
    def __init__(self) -> None:
        super().__init__("exploding")

    def handle(self, frame: Frame, context: PipelineContext) -> Frame:
        raise RuntimeError("boom")


class SplitProcessor(FrameProcessorBase):
    def handle(self, frame: Frame, context: PipelineContext) -> list[Frame]:
        return [frame, MetricsFrame(frame.call_id, "split", 1.0)]


@dataclass
class FakeTranscriptionResult:
    text: str
    is_final: bool = True
    reason: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class FakeStreamingSTT:
    def __init__(self, results: list[FakeTranscriptionResult]) -> None:
        self.results = results

    def transcribe(self, call_audio):
        return self.results[-1]

    def transcribe_stream(self, call_audio):
        return iter(self.results)


class FakeStreamingTTS:
    def synthesize(self, text: str):
        return np.ones(4, dtype=np.float32), 0.25

    def synthesize_stream(self, text: str):
        yield np.ones(4, dtype=np.float32), 0.25
        yield np.ones(8, dtype=np.float32), 0.5


class PipelineProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_emits_sink_frames_after_processor_chain(self) -> None:
        emitted: list[Frame] = []
        runner = PipelineRunner([PassthroughProcessor(), SplitProcessor()], sink=emitted.append)
        frame = TextFrame("system", "call-1", "hello")

        output = await runner.push(frame)

        self.assertEqual([f.kind for f in output], ["system", "metrics"])
        self.assertEqual([f.kind for f in emitted], ["system", "metrics"])

    async def test_pipeline_converts_processor_exception_to_error_frame(self) -> None:
        runner = PipelineRunner([ExplodingProcessor()])
        frame = TextFrame("system", "call-1", "hello", trace_id="trace-1")

        with self.assertLogs("voicebot.pipeline", level="ERROR"):
            output = await runner.push(frame)

        self.assertEqual(len(output), 1)
        self.assertEqual(output[0].kind, "error")
        self.assertEqual(output[0].call_id, "call-1")
        self.assertEqual(output[0].trace_id, "trace-1")
        self.assertEqual(output[0].data["processor"], "exploding")

    async def test_drop_processor_stops_current_frame(self) -> None:
        runner = PipelineRunner([DropProcessor()])

        output = await runner.push(TextFrame("system", "call-1", "drop me"))

        self.assertEqual(output, [])

    async def test_runner_closes_processors(self) -> None:
        closed = False

        class ClosableProcessor(FrameProcessorBase):
            async def close(self) -> None:
                nonlocal closed
                closed = True

        runner = PipelineRunner([ClosableProcessor()])

        await runner.close()

        self.assertTrue(closed)


class CoreProcessorTests(unittest.TestCase):
    def test_stt_processor_emits_partial_and_final_transcription_frames(self) -> None:
        stt = FakeStreamingSTT(
            [
                FakeTranscriptionResult("hel", is_final=False, metadata={"confidence": 0.4}),
                FakeTranscriptionResult("hello", is_final=True, metadata={"confidence": 0.9}),
            ]
        )
        processor = STTProcessor(stt)
        frame = AudioInputFrame(
            "call-1",
            np.zeros(160, dtype=np.float32),
            8000,
            trace_id="trace-1",
            data={"turn_id": 7},
        )

        output = list(processor.handle(frame, PipelineContext()))

        self.assertEqual([f.kind for f in output], ["transcription_started", "transcription_partial", "transcription_finished", "user_transcript"])
        self.assertEqual(output[1].text, "hel")
        self.assertEqual(output[3].text, "hello")
        self.assertEqual(output[3].data["turn_id"], 7)
        self.assertEqual(output[3].trace_id, "trace-1")

    def test_stt_processor_emits_empty_when_stream_has_no_final_text(self) -> None:
        processor = STTProcessor(FakeStreamingSTT([FakeTranscriptionResult("", is_final=True, reason="silence")]))
        frame = AudioInputFrame("call-1", np.zeros(160, dtype=np.float32), 8000, data={"turn_id": 3})

        output = list(processor.handle(frame, PipelineContext()))

        self.assertEqual([f.kind for f in output], ["transcription_started", "transcription_empty"])
        self.assertEqual(output[1].data["reason"], "silence")

    def test_agent_request_processor_ignores_partials_by_default(self) -> None:
        processor = AgentRequestProcessor()
        partial = TranscriptionFrame("transcription_partial", "call-1", 1, text="hel")

        output = processor.handle(partial, PipelineContext())

        self.assertIs(output, partial)

    def test_agent_request_processor_can_request_on_partials(self) -> None:
        processor = AgentRequestProcessor(request_partials=True)
        partial = TranscriptionFrame("transcription_partial", "call-1", 1, text="hel")

        output = list(processor.handle(partial, PipelineContext()))

        self.assertEqual([f.kind for f in output], ["transcription_partial", "agent_request"])
        self.assertEqual(output[1].text, "hel")
        self.assertTrue(output[1].data["partial"])

    def test_agent_request_processor_creates_request_for_final_transcript(self) -> None:
        processor = AgentRequestProcessor()
        transcript = TranscriptionFrame("user_transcript", "call-1", 2, text="hello")

        output = list(processor.handle(transcript, PipelineContext()))

        self.assertEqual([f.kind for f in output], ["user_transcript", "agent_request"])
        self.assertEqual(output[1].data["turn_id"], 2)
        self.assertEqual(output[1].data["transcript_frame_id"], transcript.frame_id)

    def test_tts_processor_emits_audio_chunks_and_finished_frame(self) -> None:
        processor = TTSProcessor(FakeStreamingTTS())
        response = TextFrame("agent_response", "call-1", "hello", response_to_frame_id="req-1")

        output = list(processor.handle(response, PipelineContext()))

        self.assertEqual([f.kind for f in output], ["agent_response", "tts_started", "audio_output", "audio_output", "tts_finished"])
        self.assertTrue(all(isinstance(f, AudioOutputFrame) for f in output[2:4]))
        self.assertEqual(output[-1].data["duration"], 0.75)
        self.assertFalse(output[-1].data["partial"])

    def test_event_log_processor_persists_known_frame_events(self) -> None:
        events = EventStore(max_context_events=20)
        processor = EventLogProcessor(events)
        frame = MetricsFrame("call-1", "stt_duration_seconds", 0.12, data={"turn_id": 1})

        output = processor.handle(frame, PipelineContext())

        self.assertIs(output, frame)
        persisted = events.list_events(call_id="call-1")
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0].type, "metrics")
        self.assertEqual(persisted[0].data["name"], "stt_duration_seconds")


class ProcessorRegistryTests(unittest.TestCase):
    def test_default_registry_creates_configured_processors(self) -> None:
        registry = default_processor_registry()
        events = EventStore(max_context_events=20)
        processors = registry.create_many(
            [
                ProcessorSpec("event-log"),
                ProcessorSpec("stt"),
                ProcessorSpec("agent-request", {"request_partials": True}),
                ProcessorSpec("tts"),
            ],
            ProcessorDependencies(events=events, stt=FakeStreamingSTT([]), tts=FakeStreamingTTS()),
        )

        self.assertEqual([p.name for p in processors], ["event-log", "stt", "agent-request", "tts"])
        self.assertTrue(processors[2].request_partials)

    def test_registry_reports_unknown_processor_names(self) -> None:
        registry = default_processor_registry()

        with self.assertRaisesRegex(ValueError, "Unknown processor 'missing'"):
            registry.create(ProcessorSpec("missing"), ProcessorDependencies())

    def test_registry_requires_declared_dependencies(self) -> None:
        registry = default_processor_registry()

        with self.assertRaisesRegex(ValueError, "stt processor requires STT dependency"):
            registry.create(ProcessorSpec("stt"), ProcessorDependencies())

    def test_registry_creates_fanout_with_branch_processors(self) -> None:
        registry = default_processor_registry()
        processor = registry.create(
            ProcessorSpec(
                "fan-out",
                {
                    "name": "observer-fanout",
                    "branches": [
                        {
                            "name": "observer",
                            "include_outputs": True,
                            "processors": [{"name": "passthrough", "options": {"name": "observer-pass"}}],
                        },
                        {
                            "name": "dropper",
                            "processors": [{"name": "drop"}],
                        },
                    ],
                },
            ),
            ProcessorDependencies(),
        )

        self.assertIsInstance(processor, FanOutProcessor)
        self.assertEqual(processor.name, "observer-fanout")
        self.assertEqual([branch.name for branch in processor.branches], ["observer", "dropper"])
        self.assertTrue(processor.branches[0].include_outputs)

    def test_registry_rejects_invalid_fanout_branch_config(self) -> None:
        registry = default_processor_registry()

        with self.assertRaisesRegex(ValueError, "fan-out branch processors must be a list"):
            registry.create(
                ProcessorSpec("fan-out", {"branches": [{"name": "bad", "processors": "passthrough"}]}),
                ProcessorDependencies(),
            )


if __name__ == "__main__":
    unittest.main()
