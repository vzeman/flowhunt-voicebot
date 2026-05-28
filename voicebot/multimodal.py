from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Modality = Literal[
    "audio",
    "text",
    "image",
    "video",
    "screen",
    "file",
    "chat",
    "visual_card",
    "avatar_video",
]

ContentDirection = Literal["input", "output"]


@dataclass(frozen=True)
class MultimodalContent:
    modality: Modality
    direction: ContentDirection
    mime_type: str | None = None
    uri: str | None = None
    text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_agent_part(self) -> dict[str, Any]:
        part = {
            "modality": self.modality,
            "direction": self.direction,
            "mime_type": self.mime_type,
            "metadata": self.metadata,
        }
        if self.uri:
            part["uri"] = self.uri
        if self.text:
            part["text"] = self.text
        return {key: value for key, value in part.items() if value is not None}


@dataclass(frozen=True)
class MultimodalContext:
    call_id: str
    workspace_id: str | None = None
    voicebot_id: str | None = None
    session_id: str | None = None
    parts: tuple[MultimodalContent, ...] = ()

    def add(self, part: MultimodalContent) -> "MultimodalContext":
        return MultimodalContext(
            call_id=self.call_id,
            workspace_id=self.workspace_id,
            voicebot_id=self.voicebot_id,
            session_id=self.session_id,
            parts=(*self.parts, part),
        )

    def to_agent_context(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "workspace_id": self.workspace_id,
            "voicebot_id": self.voicebot_id,
            "session_id": self.session_id,
            "parts": [part.to_agent_part() for part in self.parts],
        }


class MultimodalContextStore:
    def __init__(self) -> None:
        self._contexts: dict[str, MultimodalContext] = {}

    def add_part(
        self,
        call_id: str,
        part: MultimodalContent,
        *,
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
        session_id: str | None = None,
    ) -> MultimodalContext:
        context = self._contexts.get(call_id) or MultimodalContext(
            call_id=call_id,
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            session_id=session_id,
        )
        context = MultimodalContext(
            call_id=call_id,
            workspace_id=context.workspace_id or workspace_id,
            voicebot_id=context.voicebot_id or voicebot_id,
            session_id=context.session_id or session_id,
            parts=(*context.parts, part),
        )
        self._contexts[call_id] = context
        return context

    def get(self, call_id: str) -> MultimodalContext:
        return self._contexts.get(call_id) or MultimodalContext(call_id=call_id)


@dataclass(frozen=True)
class ModalityCapabilities:
    input: frozenset[Modality] = frozenset({"audio", "text"})
    output: frozenset[Modality] = frozenset({"audio", "text"})

    def supports_input(self, modality: Modality) -> bool:
        return modality in self.input

    def supports_output(self, modality: Modality) -> bool:
        return modality in self.output

    def to_dict(self) -> dict[str, list[str]]:
        return {"input": sorted(self.input), "output": sorted(self.output)}
