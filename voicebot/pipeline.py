from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
import inspect
import logging
from typing import Protocol

from .frames import ErrorFrame, Frame


logger = logging.getLogger(__name__)


class FrameProcessor(Protocol):
    name: str

    def process(self, frame: Frame, context: PipelineContext) -> Frame | Iterable[Frame] | None | Awaitable[Frame | Iterable[Frame] | None]:
        raise NotImplementedError


FrameSink = Callable[[Frame], None | Awaitable[None]]


@dataclass
class PipelineContext:
    stopped: bool = False
    metadata: dict[str, object] = field(default_factory=dict)

    def stop(self) -> None:
        self.stopped = True


class PipelineRunner:
    def __init__(
        self,
        processors: Iterable[FrameProcessor],
        *,
        sink: FrameSink | None = None,
        context: PipelineContext | None = None,
    ) -> None:
        self.processors = list(processors)
        self.sink = sink
        self.context = context or PipelineContext()

    async def push(self, frame: Frame) -> list[Frame]:
        frames = [frame]
        for processor in self.processors:
            if self.context.stopped:
                break
            next_frames: list[Frame] = []
            for current in frames:
                result = await self._process_one(processor, current)
                next_frames.extend(_coerce_frames(result))
            frames = next_frames
            if not frames:
                break

        for output in frames:
            await self._emit(output)
        return frames

    async def close(self) -> None:
        self.context.stop()
        for processor in self.processors:
            close = getattr(processor, "close", None)
            if close is None:
                continue
            result = close()
            if inspect.isawaitable(result):
                await result

    async def _process_one(self, processor: FrameProcessor, frame: Frame) -> Frame | Iterable[Frame] | None:
        try:
            result = processor.process(frame, self.context)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception as exc:
            processor_name = getattr(processor, "name", processor.__class__.__name__)
            logger.exception("frame processor failed: %s", processor_name)
            return ErrorFrame(
                frame.call_id,
                str(exc),
                trace_id=frame.trace_id,
                data={"processor": processor_name, "source_frame_id": frame.frame_id},
            )

    async def _emit(self, frame: Frame) -> None:
        if self.sink is None:
            return
        result = self.sink(frame)
        if inspect.isawaitable(result):
            await result


def _coerce_frames(value: Frame | Iterable[Frame] | None) -> list[Frame]:
    if value is None:
        return []
    if isinstance(value, Frame):
        return [value]
    return list(value)
