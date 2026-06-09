from __future__ import annotations

from dataclasses import dataclass
import html
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .events import event_to_dict


@dataclass(frozen=True)
class DashboardApiContext:
    settings: Any
    events: Any
    voicebot_store: Any
    channel_resolver: Any
    public_route_store: Any
    voicebot_session_store: Any
    webrtc: Any
    dashboard_page: str
    webrtc_test_page: str


def create_dashboard_router(context: DashboardApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/dashboard")
    def dashboard() -> HTMLResponse:
        return HTMLResponse(
            context.dashboard_page.replace("__WEBRTC_CONSOLE_SRCDOC__", html.escape(context.webrtc_test_page, quote=True)),
            headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
        )

    @router.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard")

    @router.get("/dashboard/state")
    def dashboard_state(request: Request, workspace_id: str | None = None) -> dict[str, Any]:
        dashboard_user = getattr(request.state, "dashboard_user", None)
        workspace_ids = dashboard_visible_workspace_ids(context, dashboard_user)
        selected_workspace = workspace_id or (workspace_ids[0] if workspace_ids else "")
        if selected_workspace and selected_workspace not in workspace_ids:
            if context.settings.dashboard_auth_enabled:
                raise HTTPException(status_code=403, detail="dashboard_workspace_access_denied")
            selected_workspace = workspace_ids[0] if workspace_ids else ""
        voicebot_rows = []
        if selected_workspace:
            active_counts = active_session_counts_by_voicebot(context)
            for voicebot in context.voicebot_store.list(selected_workspace):
                channels_for_voicebot = context.channel_resolver.bindings_for_voicebot(
                    selected_workspace,
                    voicebot.voicebot_id,
                )
                routes_for_voicebot = context.public_route_store.list(selected_workspace, voicebot.voicebot_id)
                voicebot_rows.append(
                    {
                        **voicebot.as_dict(),
                        "channels": [binding.as_dict() for binding in channels_for_voicebot],
                        "public_routes": [route.as_dict() for route in routes_for_voicebot],
                        "active_sessions": active_counts.get((selected_workspace, voicebot.voicebot_id), 0),
                    }
                )
        return {
            "dashboard": {
                "access": "internal",
                "auth": "dashboard_user_login" if context.settings.dashboard_auth_enabled else "local_internal",
                "user": dashboard_user,
                "webrtc_console": "embedded",
            },
            "workspaces": workspace_ids,
            "workspace_rows": dashboard_workspace_rows(context, workspace_ids),
            "selected_workspace_id": selected_workspace,
            "voicebots": voicebot_rows,
            "active_sessions": dashboard_session_rows(context, active_only=True),
            "session_history": dashboard_session_rows(context, include_active_snapshots=True),
            "recent_events": [
                event_to_dict(event)
                for event in context.events.list_events(limit=80, workspace_id=selected_workspace or None)
            ],
        }

    return router


def active_session_counts_by_voicebot(context: DashboardApiContext) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    if context.webrtc is None:
        return counts
    for snapshot in context.webrtc.snapshots():
        metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
        route_data = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
        workspace_id = metadata.get("workspace_id") or route_data.get("workspace_id")
        voicebot_id = metadata.get("voicebot_id") or route_data.get("voicebot_id")
        if workspace_id and voicebot_id:
            key = (str(workspace_id), str(voicebot_id))
            counts[key] = counts.get(key, 0) + 1
    return counts


def dashboard_workspace_rows(context: DashboardApiContext, workspace_ids: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for workspace_id in workspace_ids:
        display_names = [
            voicebot.display_name
            for voicebot in context.voicebot_store.list(workspace_id)
            if voicebot.display_name
        ]
        rows.append(
            {
                "workspace_id": workspace_id,
                "name": workspace_id if not display_names else workspace_id,
            }
        )
    return rows


def dashboard_session_rows(
    context: DashboardApiContext,
    active_only: bool = False,
    ended_only: bool = False,
    include_active_snapshots: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for session in context.voicebot_session_store.list(active_only=active_only):
        if ended_only and session.status != "ended":
            continue
        rows.append(session.as_dict())
    if (active_only or include_active_snapshots) and context.webrtc is not None:
        known_session_ids = {str(row.get("session_id")) for row in rows}
        for snapshot in context.webrtc.snapshots():
            session_id = str(snapshot.get("session_id") or "")
            if not session_id or session_id in known_session_ids:
                continue
            metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
            route = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else {}
            workspace_id = str(metadata.get("workspace_id") or route.get("workspace_id") or "")
            voicebot_id = str(metadata.get("voicebot_id") or route.get("voicebot_id") or "")
            if not workspace_id or not voicebot_id:
                continue
            rows.append(
                {
                    "session_id": session_id,
                    "workspace_id": workspace_id,
                    "voicebot_id": voicebot_id,
                    "channel_id": metadata.get("channel_id") or route.get("channel_id"),
                    "external_session_id": snapshot.get("call_id"),
                    "status": "active",
                    "started_at": metadata.get("started_at") or "",
                    "ended_at": None,
                    "metadata": {"transport": snapshot.get("transport"), **metadata},
                }
            )
    return sorted(rows, key=lambda item: str(item.get("started_at") or item.get("session_id") or ""), reverse=True)


def dashboard_user_auth(context: DashboardApiContext, request: Request) -> dict[str, Any]:
    if not context.settings.dashboard_auth_enabled:
        return {
            "ok": True,
            "user": {
                "user_id": "local-dashboard",
                "workspace_ids": list(context.voicebot_store.workspace_ids()),
                "dev_login": False,
            },
        }
    if dashboard_dev_login_allowed(context, request):
        return {
            "ok": True,
            "user": {
                "user_id": "dev-dashboard-user",
                "workspace_ids": list(context.voicebot_store.workspace_ids()),
                "dev_login": True,
            },
        }
    user_id = request.headers.get(context.settings.dashboard_user_id_header, "").strip()
    if not user_id:
        return {"ok": False, "status_code": 401, "reason": "dashboard_login_required"}
    workspace_ids = [
        item.strip()
        for item in request.headers.get(context.settings.dashboard_workspace_ids_header, "").split(",")
        if item.strip()
    ]
    return {
        "ok": True,
        "user": {
            "user_id": user_id,
            "workspace_ids": sorted(set(workspace_ids)),
            "dev_login": False,
        },
    }


def dashboard_dev_login_allowed(context: DashboardApiContext, request: Request) -> bool:
    if not context.settings.dashboard_dev_login_enabled:
        return False
    if context.settings.deployment_mode not in {"local", "development", "dev", "test"}:
        return False
    return request.headers.get("X-FlowHunt-Dev-Login", "").lower() in {"1", "true", "yes"}


def dashboard_visible_workspace_ids(
    context: DashboardApiContext,
    dashboard_user: dict[str, Any] | None,
) -> list[str]:
    all_workspaces = list(context.voicebot_store.workspace_ids())
    if not context.settings.dashboard_auth_enabled or not dashboard_user:
        return all_workspaces
    allowed = set(dashboard_user.get("workspace_ids") or [])
    if dashboard_user.get("dev_login"):
        return all_workspaces
    return [workspace_id for workspace_id in all_workspaces if workspace_id in allowed]
