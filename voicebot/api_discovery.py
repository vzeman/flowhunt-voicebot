from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.openapi.utils import get_openapi

from .api_audience import filter_routes_by_audience, route_audience_inventory
from .api_surface import (
    api_scope_violations,
    api_surface_by_area,
    api_surface_integrity_issues,
    api_surface_summary,
    prototype_endpoints,
    public_endpoints_are_workspace_scoped,
)


@dataclass(frozen=True)
class DiscoveryApiContext:
    app: FastAPI


def create_discovery_router(context: DiscoveryApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/api/surface")
    def api_surface() -> dict[str, Any]:
        return {
            "summary": api_surface_summary(),
            "areas": api_surface_by_area(),
            "public_endpoints_are_workspace_scoped": public_endpoints_are_workspace_scoped(),
            "scope_violations": api_scope_violations(),
            "integrity_issues": api_surface_integrity_issues(),
            "route_audiences": route_audience_inventory(context.app.routes),
        }

    @router.get("/openapi/public.json", include_in_schema=False)
    def public_openapi() -> dict[str, Any]:
        return audience_openapi("public")

    @router.get("/openapi/internal.json", include_in_schema=False)
    def internal_openapi() -> dict[str, Any]:
        return audience_openapi("internal", include_local_dev=True)

    def audience_openapi(audience: str, include_local_dev: bool = False) -> dict[str, Any]:
        return get_openapi(
            title=f"{context.app.title} {audience.title()} API",
            version=context.app.version,
            routes=filter_routes_by_audience(
                context.app.routes,
                audience,
                include_local_dev=include_local_dev,
            ),  # type: ignore[arg-type]
        )

    @router.get("/api/surface/prototypes")
    def api_surface_prototypes() -> dict[str, Any]:
        return {"endpoints": prototype_endpoints()}

    return router
