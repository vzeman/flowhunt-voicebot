from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ChannelKind = Literal["sip_trunk", "phone_number", "webrtc_widget"]


@dataclass(frozen=True)
class WorkspaceScope:
    workspace_id: str
    voicebot_id: str
    session_id: str | None = None

    def __post_init__(self) -> None:
        if not self.workspace_id:
            raise ValueError("workspace_id is required")
        if not self.voicebot_id:
            raise ValueError("voicebot_id is required")

    def event_data(self) -> dict[str, str]:
        data = {"workspace_id": self.workspace_id, "voicebot_id": self.voicebot_id}
        if self.session_id:
            data["session_id"] = self.session_id
        return data

    def task_dedupe_key(self, request_event_id: int) -> str:
        if not self.session_id:
            raise ValueError("session_id is required for task dedupe")
        return f"{self.session_id}:{request_event_id}"


@dataclass(frozen=True)
class VoicebotChannelBinding:
    channel_id: str
    kind: ChannelKind
    workspace_id: str
    voicebot_id: str
    external_id: str
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def scope(self) -> WorkspaceScope:
        return WorkspaceScope(self.workspace_id, self.voicebot_id)


class ChannelResolver:
    def __init__(self, bindings: list[VoicebotChannelBinding] | None = None) -> None:
        self._bindings: dict[tuple[ChannelKind, str], VoicebotChannelBinding] = {}
        for binding in bindings or []:
            self.register(binding)

    def register(self, binding: VoicebotChannelBinding) -> None:
        self._bindings[(binding.kind, binding.external_id)] = binding

    def resolve(self, kind: ChannelKind, external_id: str) -> WorkspaceScope | None:
        binding = self._bindings.get((kind, external_id))
        if binding is None or not binding.enabled:
            return None
        return binding.scope()

    def bindings_for_workspace(self, workspace_id: str) -> list[VoicebotChannelBinding]:
        return sorted(
            [binding for binding in self._bindings.values() if binding.workspace_id == workspace_id],
            key=lambda item: item.channel_id,
        )


def require_same_workspace(source: WorkspaceScope, target_workspace_id: str) -> None:
    if source.workspace_id != target_workspace_id:
        raise ValueError("cross-workspace voicebot operation is not allowed")
