from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field

from .frames import Frame
from .pipeline import FrameSink, PipelineContext, PipelineRunner
from .processors import FrameProcessorBase, ProcessorResult


@dataclass
class FanOutBranch:
    name: str
    processors: Iterable[FrameProcessorBase]
    sink: FrameSink | None = None
    include_outputs: bool = False
    context: PipelineContext = field(default_factory=PipelineContext)


class FanOutProcessor(FrameProcessorBase):
    def __init__(self, branches: Iterable[FanOutBranch], *, name: str = "fan-out") -> None:
        super().__init__(name)
        self.branches = list(branches)
        self._runners = [
            PipelineRunner(branch.processors, sink=branch.sink, context=branch.context)
            for branch in self.branches
        ]

    async def handle(self, frame: Frame, context: PipelineContext) -> ProcessorResult:
        if not self._runners:
            return frame

        branch_outputs = await asyncio.gather(*(runner.push(frame) for runner in self._runners))
        output: list[Frame] = [frame]
        for branch, frames in zip(self.branches, branch_outputs, strict=True):
            if branch.include_outputs:
                output.extend(frames)
        return output

    async def close(self) -> None:
        await asyncio.gather(*(runner.close() for runner in self._runners))
