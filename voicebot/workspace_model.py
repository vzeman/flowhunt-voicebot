from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
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
        route_key = (binding.kind, binding.external_id)
        existing_route = self._bindings.get(route_key)
        if existing_route is not None and existing_route.channel_id != binding.channel_id:
            raise ValueError("cannot reassign channel route to another channel")
        for existing_key, existing_binding in self._bindings.items():
            if existing_binding.channel_id != binding.channel_id:
                continue
            if existing_key != route_key:
                raise ValueError("cannot move channel binding across routes")
            if existing_binding.workspace_id != binding.workspace_id:
                raise ValueError("cannot move channel binding across workspaces")
            if existing_binding.voicebot_id != binding.voicebot_id:
                raise ValueError("cannot move channel binding across voicebots")
        self._bindings[route_key] = binding

    def unregister(self, kind: ChannelKind, external_id: str) -> VoicebotChannelBinding | None:
        return self._bindings.pop((kind, external_id), None)

    def unregister_channel(self, channel_id: str) -> VoicebotChannelBinding | None:
        for key, binding in list(self._bindings.items()):
            if binding.channel_id == channel_id:
                return self._bindings.pop(key)
        return None

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


@dataclass(frozen=True)
class VoicebotSessionRecord:
    session_id: str
    workspace_id: str
    voicebot_id: str
    channel_id: str | None = None
    external_session_id: str | None = None
    status: Literal["active", "ended"] = "active"
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    ended_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id is required")
        WorkspaceScope(self.workspace_id, self.voicebot_id, self.session_id)

    def scope(self) -> WorkspaceScope:
        return WorkspaceScope(self.workspace_id, self.voicebot_id, self.session_id)

    def end(self, ended_at: str | None = None) -> "VoicebotSessionRecord":
        return VoicebotSessionRecord(
            session_id=self.session_id,
            workspace_id=self.workspace_id,
            voicebot_id=self.voicebot_id,
            channel_id=self.channel_id,
            external_session_id=self.external_session_id,
            status="ended",
            started_at=self.started_at,
            ended_at=ended_at or datetime.now(UTC).isoformat(),
            metadata=self.metadata,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
            "voicebot_id": self.voicebot_id,
            "channel_id": self.channel_id,
            "external_session_id": self.external_session_id,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "metadata": self.metadata,
        }


class VoicebotSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, VoicebotSessionRecord] = {}

    def save(self, session: VoicebotSessionRecord) -> VoicebotSessionRecord:
        existing = self._sessions.get(session.session_id)
        if existing is not None and existing.workspace_id != session.workspace_id:
            raise ValueError("cannot move voicebot session across workspaces")
        if existing is not None and existing.voicebot_id != session.voicebot_id:
            raise ValueError("cannot move voicebot session across voicebots")
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str, workspace_id: str | None = None) -> VoicebotSessionRecord | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if workspace_id is not None and session.workspace_id != workspace_id:
            return None
        return session

    def end(self, session_id: str, workspace_id: str) -> VoicebotSessionRecord:
        session = self.get(session_id, workspace_id)
        if session is None:
            raise KeyError(f"unknown session in workspace {workspace_id}: {session_id}")
        return self.save(session.end())

    def list(
        self,
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
        active_only: bool = False,
    ) -> tuple[VoicebotSessionRecord, ...]:
        return tuple(
            session
            for session in sorted(self._sessions.values(), key=lambda item: item.session_id)
            if (workspace_id is None or session.workspace_id == workspace_id)
            and (voicebot_id is None or session.voicebot_id == voicebot_id)
            and (not active_only or session.status == "active")
        )


def require_same_workspace(source: WorkspaceScope, target_workspace_id: str) -> None:
    if source.workspace_id != target_workspace_id:
        raise ValueError("cross-workspace voicebot operation is not allowed")
