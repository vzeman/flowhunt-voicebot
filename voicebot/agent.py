from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .frames import TextFrame


@dataclass(frozen=True)
class AgentToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentOutput:
    text: str = ""
    tool_calls: list[AgentToolCall] = field(default_factory=list)
    is_final: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str) -> AgentOutput:
        raise NotImplementedError

    def generate_stream(self, prompt: str) -> Iterable[AgentOutput]:
        yield self.generate(prompt)


def merge_agent_stream(chunks: Iterable[AgentOutput]) -> AgentOutput:
    text_parts: list[str] = []
    tool_calls: list[AgentToolCall] = []
    metadata: dict[str, Any] = {}
    saw_chunk = False
    for chunk in chunks:
        saw_chunk = True
        if chunk.text:
            text_parts.append(chunk.text)
        tool_calls.extend(chunk.tool_calls)
        metadata.update(chunk.metadata)
    if not saw_chunk:
        return AgentOutput(metadata={"reason": "empty_stream"})
    return AgentOutput(text="".join(text_parts).strip(), tool_calls=tool_calls, metadata=metadata)


def agent_output_to_frame(output: AgentOutput, call_id: str, *, trace_id: str | None = None) -> TextFrame:
    kind = "agent_response" if output.is_final else "agent_response_partial"
    return TextFrame(kind, call_id, output.text, trace_id=trace_id, data={"metadata": output.metadata})
