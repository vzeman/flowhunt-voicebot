from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fastapi.routing import APIRoute


ApiAudience = Literal["public", "internal", "local_dev"]


@dataclass(frozen=True)
class RouteAudience:
    audience: ApiAudience
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"audience": self.audience, "reason": self.reason}


PUBLIC_ROUTE_KEYS = {
    ("GET", "/.well-known/flowhunt-voicebot"),
    ("GET", "/health"),
    ("GET", "/health/liveness"),
    ("GET", "/openapi/public.json"),
    ("GET", "/widget"),
    ("GET", "/widget.js"),
    ("DELETE", "/webrtc/sessions/{session_id}"),
    ("POST", "/webrtc/sessions"),
}

LOCAL_DEV_ROUTE_KEYS = {
    ("GET", "/webrtc/test"),
}

INTERNAL_ROUTE_PREFIXES = (
    "/agent",
    "/api/surface",
    "/calls",
    "/config",
    "/context",
    "/dashboard",
    "/deployment",
    "/events",
    "/health",
    "/metrics",
    "/multimodal",
    "/observability",
    "/operations",
    "/openapi",
    "/pipeline",
    "/providers",
    "/realtime",
    "/routing",
    "/scaling",
    "/security",
    "/sip",
    "/sip-trunks",
    "/storage",
    "/subagent",
    "/transcripts",
    "/webrtc",
    "/workspaces",
)


def classify_route(method: str, path: str) -> RouteAudience | None:
    key = (method.upper(), path)
    if key in PUBLIC_ROUTE_KEYS:
        return RouteAudience("public", "caller-safe public runtime endpoint")
    if key in LOCAL_DEV_ROUTE_KEYS:
        return RouteAudience("local_dev", "local browser test page")
    if any(path == prefix or path.startswith(f"{prefix}/") for prefix in INTERNAL_ROUTE_PREFIXES):
        return RouteAudience("internal", "internal control-plane or operations endpoint")
    return None


def apply_route_audiences(routes: list) -> list[dict[str, object]]:
    unclassified: list[dict[str, object]] = []
    for route in routes:
        if not isinstance(route, APIRoute):
            continue
        audiences: set[ApiAudience] = set()
        reasons: set[str] = set()
        for method in sorted(route.methods or ()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            classification = classify_route(method, route.path)
            if classification is None:
                unclassified.append({"method": method, "path": route.path, "name": route.name})
                continue
            audiences.add(classification.audience)
            reasons.add(classification.reason)
        if not audiences:
            continue
        if len(audiences) > 1:
            unclassified.append(
                {"methods": sorted(route.methods or ()), "path": route.path, "name": route.name, "reason": "mixed audiences"}
            )
            continue
        audience = next(iter(audiences))
        route.openapi_extra = {
            **(route.openapi_extra or {}),
            "x-voicebot-audience": audience,
            "x-voicebot-audience-reason": "; ".join(sorted(reasons)),
        }
        setattr(route, "voicebot_audience", audience)
    return unclassified


def route_audience(route: APIRoute) -> ApiAudience | None:
    value = getattr(route, "voicebot_audience", None)
    return value if value in {"public", "internal", "local_dev"} else None


def route_audience_inventory(routes: list) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    for route in routes:
        if not isinstance(route, APIRoute):
            continue
        inventory.append(
            {
                "path": route.path,
                "methods": sorted(method for method in (route.methods or ()) if method not in {"HEAD", "OPTIONS"}),
                "name": route.name,
                "audience": route_audience(route),
            }
        )
    return sorted(inventory, key=lambda item: (str(item["path"]), ",".join(item["methods"])))


def filter_routes_by_audience(routes: list, audience: ApiAudience, include_local_dev: bool = False) -> list:
    allowed = {audience}
    if include_local_dev:
        allowed.add("local_dev")
    return [route for route in routes if not isinstance(route, APIRoute) or route_audience(route) in allowed]
