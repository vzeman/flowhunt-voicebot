from __future__ import annotations

from typing import Any, Literal

from .session_leases import SessionLease, SessionLeaseStore


OwnershipStatus = Literal["owned", "missing", "owner_mismatch", "unscoped"]


def audit_session_ownership(
    snapshots: list[dict[str, Any]],
    leases: SessionLeaseStore,
    expected_owner: str | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for snapshot in snapshots:
        identity = session_identity_from_snapshot(snapshot)
        if identity is None:
            rows.append(
                {
                    "status": "unscoped",
                    "call_id": _non_empty_str(snapshot.get("call_id")),
                    "reason": "missing_workspace_voicebot_or_call_id",
                }
            )
            continue
        lease = leases.get(identity["workspace_id"], identity["voicebot_id"], identity["session_id"])
        rows.append(
            {
                **identity,
                "status": _ownership_status(lease, expected_owner),
                "expected_owner": expected_owner,
                "current_owner": lease.owner if lease is not None else None,
                "lease": lease.as_dict() if lease is not None else None,
                "reason": _ownership_reason(lease, expected_owner),
            }
        )
    return sorted(rows, key=lambda row: str(row.get("call_id") or ""))


def session_identity_from_snapshot(snapshot: dict[str, Any]) -> dict[str, str] | None:
    route = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
    workspace_id = _non_empty_str(route.get("workspace_id") or snapshot.get("workspace_id"))
    voicebot_id = _non_empty_str(route.get("voicebot_id") or snapshot.get("voicebot_id"))
    call_id = _non_empty_str(snapshot.get("call_id"))
    if workspace_id is None or voicebot_id is None or call_id is None:
        return None
    session_id = _non_empty_str(snapshot.get("session_id")) or call_id
    return {
        "workspace_id": workspace_id,
        "voicebot_id": voicebot_id,
        "session_id": session_id,
        "call_id": call_id,
        "transport": str(snapshot.get("transport") or ""),
    }


def _ownership_status(lease: SessionLease | None, expected_owner: str | None) -> OwnershipStatus:
    if lease is None:
        return "missing"
    if expected_owner is not None and lease.owner != expected_owner:
        return "owner_mismatch"
    return "owned"


def _ownership_reason(lease: SessionLease | None, expected_owner: str | None) -> str:
    if lease is None:
        return "lease_missing"
    if expected_owner is not None and lease.owner != expected_owner:
        return "lease_owner_mismatch"
    return "lease_owner_current"


def _non_empty_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
