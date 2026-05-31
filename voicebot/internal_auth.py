from __future__ import annotations

from dataclasses import dataclass
from hmac import compare_digest

from .api_audience import ApiAudience, classify_route


@dataclass(frozen=True)
class InternalApiKey:
    key_id: str
    secret: str
    service: str = "internal"
    scopes: frozenset[str] = frozenset({"internal:*"})

    def can_access(self, scope: str) -> bool:
        return "internal:*" in self.scopes or scope in self.scopes

    def public_metadata(self) -> dict[str, object]:
        return {
            "key_id": self.key_id,
            "service": self.service,
            "scopes": sorted(self.scopes),
        }


@dataclass(frozen=True)
class InternalAuthResult:
    ok: bool
    status_code: int
    reason: str
    scope: str
    key: InternalApiKey | None = None


def parse_internal_api_keys(values: tuple[str, ...]) -> tuple[InternalApiKey, ...]:
    keys: list[InternalApiKey] = []
    for index, raw in enumerate(values, start=1):
        value = raw.strip()
        if not value:
            continue
        parts = value.split(":", 3)
        if len(parts) == 1:
            keys.append(InternalApiKey(f"key-{index}", parts[0], "internal"))
            continue
        if len(parts) == 2:
            key_id, secret = parts
            keys.append(InternalApiKey(key_id.strip() or f"key-{index}", secret.strip(), "internal"))
            continue
        key_id, service, secret = parts[:3]
        scopes = frozenset(item.strip() for item in (parts[3].split("|") if len(parts) == 4 else []) if item.strip())
        keys.append(
            InternalApiKey(
                key_id.strip() or f"key-{index}",
                secret.strip(),
                service.strip() or "internal",
                scopes or frozenset({"internal:*"}),
            )
        )
    return tuple(key for key in keys if key.secret)


def validate_internal_api_key(
    provided: str | None,
    configured: tuple[InternalApiKey, ...],
    scope: str,
) -> InternalAuthResult:
    if not provided:
        return InternalAuthResult(False, 401, "missing_internal_api_key", scope)
    for key in configured:
        if compare_digest(provided, key.secret):
            if key.can_access(scope):
                return InternalAuthResult(True, 200, "accepted", scope, key)
            return InternalAuthResult(False, 403, "insufficient_internal_api_key_scope", scope, key)
    return InternalAuthResult(False, 401, "invalid_internal_api_key", scope)


def route_requires_internal_auth(method: str, path: str) -> bool:
    audience = route_audience_for_request(method, path)
    return audience in {"internal", "local_dev"}


def route_audience_for_request(method: str, path: str) -> ApiAudience:
    classification = classify_route(method, path)
    if classification is not None:
        return classification.audience
    return "internal"


def internal_scope_for_request(method: str, path: str) -> str:
    normalized = path.rstrip("/") or "/"
    if normalized.startswith("/agent"):
        return "agent:work"
    if normalized.startswith("/calls") and "/control" in normalized:
        return "call:control"
    if normalized.startswith("/calls"):
        return "call:read"
    if normalized.startswith("/workspaces"):
        return "workspace:admin"
    if normalized.startswith("/sip"):
        return "sip:manage"
    if normalized.startswith("/events") or normalized.startswith("/metrics") or normalized.startswith("/observability"):
        return "diagnostics:read"
    if normalized.startswith("/dashboard"):
        return "dashboard:read"
    if normalized.startswith("/webrtc/test"):
        return "dashboard:read"
    return "internal:read"
