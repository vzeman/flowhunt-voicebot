from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Literal, get_args


ChannelKind = Literal["sip_trunk", "phone_number", "webrtc_widget"]
VoicebotSessionStatus = Literal["active", "ended"]


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
class VoicebotDefinition:
    workspace_id: str
    voicebot_id: str
    display_name: str = ""
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.workspace_id.strip():
            raise ValueError("workspace_id is required")
        if not self.voicebot_id.strip():
            raise ValueError("voicebot_id is required")
        if self.display_name and not self.display_name.strip():
            raise ValueError("display_name must not be blank")

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "voicebot_id": self.voicebot_id,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "metadata": self.metadata,
        }


class VoicebotStore:
    def __init__(self) -> None:
        self._voicebots: dict[tuple[str, str], VoicebotDefinition] = {}

    def create(self, voicebot: VoicebotDefinition) -> VoicebotDefinition:
        key = (voicebot.workspace_id, voicebot.voicebot_id)
        if key in self._voicebots:
            raise ValueError("voicebot already exists")
        self._voicebots[key] = voicebot
        return voicebot

    def save(self, voicebot: VoicebotDefinition) -> VoicebotDefinition:
        self._voicebots[(voicebot.workspace_id, voicebot.voicebot_id)] = voicebot
        return voicebot

    def patch(
        self,
        workspace_id: str,
        voicebot_id: str,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> VoicebotDefinition:
        existing = self.get(workspace_id, voicebot_id)
        if existing is None:
            raise KeyError(f"voicebot not found: {voicebot_id}")
        updated = replace(
            existing,
            display_name=existing.display_name if display_name is None else display_name,
            enabled=existing.enabled if enabled is None else enabled,
            metadata=existing.metadata if metadata is None else metadata,
        )
        return self.save(updated)

    def get(self, workspace_id: str, voicebot_id: str) -> VoicebotDefinition | None:
        return self._voicebots.get((workspace_id, voicebot_id))

    def list(self, workspace_id: str) -> tuple[VoicebotDefinition, ...]:
        return tuple(
            sorted(
                [voicebot for voicebot in self._voicebots.values() if voicebot.workspace_id == workspace_id],
                key=lambda item: item.voicebot_id,
            )
        )

    def delete(self, workspace_id: str, voicebot_id: str) -> VoicebotDefinition | None:
        return self._voicebots.pop((workspace_id, voicebot_id), None)


@dataclass(frozen=True)
class VoicebotChannelBinding:
    channel_id: str
    kind: ChannelKind
    workspace_id: str
    voicebot_id: str
    external_id: str
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.channel_id.strip():
            raise ValueError("channel_id is required")
        if self.kind not in get_args(ChannelKind):
            raise ValueError(f"unsupported channel kind: {self.kind}")
        if not self.workspace_id.strip():
            raise ValueError("workspace_id is required")
        if not self.voicebot_id.strip():
            raise ValueError("voicebot_id is required")
        if not self.external_id.strip():
            raise ValueError("external_id is required")

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
    status: VoicebotSessionStatus = "active"
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    ended_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id is required")
        WorkspaceScope(self.workspace_id, self.voicebot_id, self.session_id)
        if self.status not in get_args(VoicebotSessionStatus):
            raise ValueError(f"unsupported voicebot session status: {self.status}")
        _parse_aware_timestamp(self.started_at, "started_at")
        if self.status == "ended" and not self.ended_at:
            raise ValueError("ended_at is required for ended voicebot sessions")
        if self.ended_at is not None:
            _parse_aware_timestamp(self.ended_at, "ended_at")

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


class JsonVoicebotSessionStore(VoicebotSessionStore):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.load_diagnostics: dict[str, int] = {
            "loaded_sessions": 0,
            "skipped_malformed_json": 0,
            "skipped_invalid_sessions": 0,
            "skipped_duplicate_session_ids": 0,
        }
        super().__init__()
        self._load()

    def save(self, session: VoicebotSessionRecord) -> VoicebotSessionRecord:
        saved = super().save(session)
        self._save()
        return saved

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            self.load_diagnostics["skipped_malformed_json"] += 1
            return
        seen: set[str] = set()
        for item in raw.get("sessions", []):
            try:
                session = voicebot_session_from_dict(item)
            except (KeyError, TypeError, ValueError):
                self.load_diagnostics["skipped_invalid_sessions"] += 1
                continue
            if session.session_id in seen:
                self.load_diagnostics["skipped_duplicate_session_ids"] += 1
                continue
            seen.add(session.session_id)
            self._sessions[session.session_id] = session
            self.load_diagnostics["loaded_sessions"] += 1

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "sessions": [session.as_dict() for session in self.list()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2))
        tmp.replace(self.path)


def voicebot_session_from_dict(data: dict[str, Any]) -> VoicebotSessionRecord:
    return VoicebotSessionRecord(
        session_id=str(data["session_id"]),
        workspace_id=str(data["workspace_id"]),
        voicebot_id=str(data["voicebot_id"]),
        channel_id=_optional_str(data.get("channel_id")),
        external_session_id=_optional_str(data.get("external_session_id")),
        status=str(data.get("status", "active")),
        started_at=str(data["started_at"]),
        ended_at=_optional_str(data.get("ended_at")),
        metadata=dict(data.get("metadata") or {}),
    )


def require_same_workspace(source: WorkspaceScope, target_workspace_id: str) -> None:
    if source.workspace_id != target_workspace_id:
        raise ValueError("cross-workspace voicebot operation is not allowed")


def _parse_aware_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} must be an ISO timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone")
    return parsed.astimezone(UTC)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
