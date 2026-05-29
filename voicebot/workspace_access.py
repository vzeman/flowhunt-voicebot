from __future__ import annotations

from dataclasses import dataclass

from .config import Settings


@dataclass(frozen=True)
class WorkspaceAccessPolicy:
    enabled: bool = False
    allowed_workspace_ids: tuple[str, ...] = ()

    def require_workspace(self, workspace_id: str) -> None:
        normalized = workspace_id.strip()
        if not normalized:
            raise ValueError("workspace_id is required")
        if not self.enabled:
            return
        if normalized not in self.allowed_workspace_ids:
            raise PermissionError("workspace access denied")


def workspace_access_policy_from_settings(settings: Settings) -> WorkspaceAccessPolicy:
    return WorkspaceAccessPolicy(
        enabled=settings.workspace_access_control_enabled,
        allowed_workspace_ids=settings.allowed_workspace_ids,
    )
