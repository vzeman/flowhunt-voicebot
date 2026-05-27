from __future__ import annotations

import unittest

from voicebot.fanout import FanOutBranch, FanOutProcessor
from voicebot.frames import Frame, MetricsFrame, TextFrame
from voicebot.pipeline import PipelineContext, PipelineRunner
from voicebot.processors import FrameProcessorBase


class RecordProcessor(FrameProcessorBase):
    def __init__(self, seen: list[Frame], name: str) -> None:
        super().__init__(name)
        self.seen = seen

    def handle(self, frame: Frame, context: PipelineContext) -> Frame:
        self.seen.append(frame)
        return frame


class MetricProcessor(FrameProcessorBase):
    def handle(self, frame: Frame, context: PipelineContext) -> list[Frame]:
        return [MetricsFrame(frame.call_id, "branch_metric", 1.0)]


class FanOutTests(unittest.IsolatedAsyncioTestCase):
    async def test_fanout_sends_same_frame_to_independent_branches(self) -> None:
        left: list[Frame] = []
        right: list[Frame] = []
        processor = FanOutProcessor(
            [
                FanOutBranch("left", [RecordProcessor(left, "left-recorder")]),
                FanOutBranch("right", [RecordProcessor(right, "right-recorder")]),
            ]
        )
        frame = TextFrame("system", "call-1", "hello")

        output = await processor.process(frame, PipelineContext())

        self.assertEqual(output, [frame])
        self.assertEqual(left, [frame])
        self.assertEqual(right, [frame])

    async def test_fanout_can_forward_selected_branch_outputs(self) -> None:
        processor = FanOutProcessor(
            [
                FanOutBranch("observer", [MetricProcessor()], include_outputs=True),
                FanOutBranch("side-effect", [MetricProcessor()]),
            ]
        )
        frame = TextFrame("system", "call-1", "hello")

        output = await processor.process(frame, PipelineContext())

        self.assertEqual([item.kind for item in output], ["system", "metrics"])
        self.assertEqual(output[1].data["name"], "branch_metric")

    async def test_fanout_branch_sinks_receive_outputs(self) -> None:
        emitted: list[Frame] = []
        processor = FanOutProcessor([FanOutBranch("observer", [MetricProcessor()], sink=emitted.append)])

        await processor.process(TextFrame("system", "call-1", "hello"), PipelineContext())

        self.assertEqual([frame.kind for frame in emitted], ["metrics"])

    async def test_fanout_runs_inside_pipeline(self) -> None:
        branch_seen: list[Frame] = []
        runner = PipelineRunner(
            [
                FanOutProcessor([FanOutBranch("observer", [RecordProcessor(branch_seen, "recorder")])]),
                MetricProcessor(),
            ]
        )
        frame = TextFrame("system", "call-1", "hello")

        output = await runner.push(frame)

        self.assertEqual([item.kind for item in output], ["metrics"])
        self.assertEqual(branch_seen, [frame])

    async def test_fanout_closes_branch_processors(self) -> None:
        closed: list[str] = []

        class ClosableProcessor(FrameProcessorBase):
            async def close(self) -> None:
                closed.append(self.name)

        processor = FanOutProcessor([FanOutBranch("closable", [ClosableProcessor("closable-processor")])])

        await processor.close()

        self.assertEqual(closed, ["closable-processor"])


if __name__ == "__main__":
    unittest.main()
