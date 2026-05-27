from __future__ import annotations

from collections.abc import Awaitable, Iterable
from typing import TypeAlias

from .frames import Frame
from .pipeline import PipelineContext


ProcessorResult: TypeAlias = Frame | Iterable[Frame] | None
MaybeAwaitableProcessorResult: TypeAlias = ProcessorResult | Awaitable[ProcessorResult]


class FrameProcessorBase:
    def __init__(self, name: str | None = None) -> None:
        self.name = name or self.__class__.__name__

    def process(self, frame: Frame, context: PipelineContext) -> MaybeAwaitableProcessorResult:
        return self.handle(frame, context)

    def handle(self, frame: Frame, context: PipelineContext) -> MaybeAwaitableProcessorResult:
        return frame

    def close(self) -> None | Awaitable[None]:
        return None

    def pass_frame(self, frame: Frame) -> Frame:
        return frame

    def drop_frame(self) -> None:
        return None

    def emit_many(self, *frames: Frame) -> list[Frame]:
        return list(frames)


class PassthroughProcessor(FrameProcessorBase):
    pass


class DropProcessor(FrameProcessorBase):
    def handle(self, frame: Frame, context: PipelineContext) -> None:
        return self.drop_frame()
