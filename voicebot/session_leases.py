from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SessionLease:
    workspace_id: str
    voicebot_id: str
    session_id: str
    owner: str
    expires_at: str
    call_id: str | None = None
    transport: str | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.workspace_id.strip():
            raise ValueError("workspace_id is required")
        if not self.voicebot_id.strip():
            raise ValueError("voicebot_id is required")
        if not self.session_id.strip():
            raise ValueError("session_id is required")
        if not self.owner.strip():
            raise ValueError("owner is required")
        _parse_time(self.expires_at)

    @property
    def lease_key(self) -> str:
        return lease_key(self.workspace_id, self.voicebot_id, self.session_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "voicebot_id": self.voicebot_id,
            "session_id": self.session_id,
            "owner": self.owner,
            "expires_at": self.expires_at,
            "lease_key": self.lease_key,
            "call_id": self.call_id,
            "transport": self.transport,
            "metadata": dict(self.metadata or {}),
        }


class SessionLeaseStore:
    def __init__(self) -> None:
        self._leases: dict[str, SessionLease] = {}

    def acquire(
        self,
        workspace_id: str,
        voicebot_id: str,
        session_id: str,
        owner: str,
        ttl_seconds: float,
        call_id: str | None = None,
        transport: str | None = None,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> SessionLease | None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        current = now or datetime.now(UTC)
        self.expire(current)
        key = lease_key(workspace_id, voicebot_id, session_id)
        existing = self._leases.get(key)
        if existing is not None and existing.owner != owner:
            return None
        lease = SessionLease(
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            session_id=session_id,
            owner=owner,
            expires_at=(current + timedelta(seconds=ttl_seconds)).isoformat(),
            call_id=call_id or (existing.call_id if existing is not None else None),
            transport=transport or (existing.transport if existing is not None else None),
            metadata=dict(metadata if metadata is not None else (existing.metadata if existing is not None else {}) or {}),
        )
        self._leases[key] = lease
        return lease

    def renew(
        self,
        workspace_id: str,
        voicebot_id: str,
        session_id: str,
        owner: str,
        ttl_seconds: float,
        call_id: str | None = None,
        transport: str | None = None,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> SessionLease | None:
        key = lease_key(workspace_id, voicebot_id, session_id)
        current = now or datetime.now(UTC)
        self.expire(current)
        existing = self._leases.get(key)
        if existing is None or existing.owner != owner:
            return None
        return self.acquire(
            workspace_id,
            voicebot_id,
            session_id,
            owner,
            ttl_seconds,
            call_id=call_id,
            transport=transport,
            metadata=metadata,
            now=current,
        )

    def release(self, workspace_id: str, voicebot_id: str, session_id: str, owner: str | None = None) -> SessionLease | None:
        key = lease_key(workspace_id, voicebot_id, session_id)
        existing = self._leases.get(key)
        if existing is None:
            return None
        if owner is not None and existing.owner != owner:
            return None
        return self._leases.pop(key)

    def get(self, workspace_id: str, voicebot_id: str, session_id: str, now: datetime | None = None) -> SessionLease | None:
        self.expire(now)
        return self._leases.get(lease_key(workspace_id, voicebot_id, session_id))

    def expire(self, now: datetime | None = None) -> tuple[SessionLease, ...]:
        current = now or datetime.now(UTC)
        expired: list[SessionLease] = []
        for key, lease in list(self._leases.items()):
            if _parse_time(lease.expires_at) > current:
                continue
            expired.append(lease)
            self._leases.pop(key, None)
        return tuple(expired)

    def list(self, workspace_id: str | None = None, voicebot_id: str | None = None, now: datetime | None = None) -> tuple[SessionLease, ...]:
        self.expire(now)
        return tuple(
            lease
            for lease in sorted(self._leases.values(), key=lambda item: item.lease_key)
            if (workspace_id is None or lease.workspace_id == workspace_id)
            and (voicebot_id is None or lease.voicebot_id == voicebot_id)
        )

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        return {"leases": [lease.as_dict() for lease in self.list(now=now)]}


class JsonSessionLeaseStore(SessionLeaseStore):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.load_diagnostics: dict[str, int] = {
            "loaded_leases": 0,
            "skipped_malformed_json": 0,
            "skipped_invalid_leases": 0,
            "skipped_duplicate_lease_keys": 0,
            "skipped_expired_leases": 0,
        }
        super().__init__()
        self._load()

    def acquire(self, *args, **kwargs) -> SessionLease | None:
        lease = super().acquire(*args, **kwargs)
        if lease is not None:
            self._save()
        return lease

    def renew(self, *args, **kwargs) -> SessionLease | None:
        lease = super().renew(*args, **kwargs)
        if lease is not None:
            self._save()
        return lease

    def release(self, *args, **kwargs) -> SessionLease | None:
        lease = super().release(*args, **kwargs)
        if lease is not None:
            self._save()
        return lease

    def expire(self, now: datetime | None = None) -> tuple[SessionLease, ...]:
        expired = super().expire(now)
        if expired:
            self._save()
        return expired

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.load_diagnostics["skipped_malformed_json"] += 1
            return
        seen: set[str] = set()
        now = datetime.now(UTC)
        for item in payload.get("leases", []):
            try:
                lease = session_lease_from_dict(item)
            except (KeyError, TypeError, ValueError):
                self.load_diagnostics["skipped_invalid_leases"] += 1
                continue
            if lease.lease_key in seen:
                self.load_diagnostics["skipped_duplicate_lease_keys"] += 1
                continue
            seen.add(lease.lease_key)
            if _parse_time(lease.expires_at) <= now:
                self.load_diagnostics["skipped_expired_leases"] += 1
                continue
            self._leases[lease.lease_key] = lease
            self.load_diagnostics["loaded_leases"] += 1

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "leases": [lease.as_dict() for lease in self.list()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def session_lease_from_dict(data: dict[str, Any]) -> SessionLease:
    return SessionLease(
        workspace_id=str(data["workspace_id"]),
        voicebot_id=str(data["voicebot_id"]),
        session_id=str(data["session_id"]),
        owner=str(data["owner"]),
        expires_at=str(data["expires_at"]),
        call_id=_optional_str(data.get("call_id")),
        transport=_optional_str(data.get("transport")),
        metadata=dict(data.get("metadata") or {}),
    )


def lease_key(workspace_id: str, voicebot_id: str, session_id: str) -> str:
    return f"{workspace_id}:{voicebot_id}:{session_id}"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
