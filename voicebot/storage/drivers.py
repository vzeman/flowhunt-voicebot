from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StoreHealth:
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, **self.details}


@dataclass(frozen=True)
class StorageDriverDefinition:
    family: str
    driver: str
    scope: str
    managed: bool
    supports_local_dev: bool
    supports_production: bool
    consistency: str
    implemented: bool = True
    idempotency_fields: tuple[str, ...] = ()
    required_scope_fields: tuple[str, ...] = ()
    notes: str = ""

    def key(self) -> tuple[str, str]:
        return self.family, normalize_driver_name(self.driver)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "driver": self.driver,
            "scope": self.scope,
            "managed": self.managed,
            "supports_local_dev": self.supports_local_dev,
            "supports_production": self.supports_production,
            "implemented": self.implemented,
            "consistency": self.consistency,
            "idempotency_fields": list(self.idempotency_fields),
            "required_scope_fields": list(self.required_scope_fields),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class StorageDriverSelection:
    family: str
    driver: str
    configured_driver: str
    path: str | None = None
    definition: StorageDriverDefinition | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "driver": self.driver,
            "configured_driver": self.configured_driver,
            "path": self.path,
            "definition": self.definition.to_dict() if self.definition else None,
            "options": redacted_options(self.options),
        }


class StorageRegistry:
    def __init__(self, definitions: list[StorageDriverDefinition] | None = None) -> None:
        self._definitions: dict[tuple[str, str], StorageDriverDefinition] = {}
        for definition in definitions or []:
            self.register(definition)

    def register(self, definition: StorageDriverDefinition) -> None:
        self._definitions[definition.key()] = definition

    def resolve(self, family: str, driver: str) -> StorageDriverDefinition:
        key = (family, normalize_driver_name(driver))
        try:
            return self._definitions[key]
        except KeyError as exc:
            supported = sorted(definition.driver for definition in self.definitions_for_family(family))
            raise ValueError(f"Unsupported storage driver for {family}: {driver}. Supported: {supported}") from exc

    def definitions_for_family(self, family: str) -> list[StorageDriverDefinition]:
        return [definition for (candidate, _driver), definition in self._definitions.items() if candidate == family]

    def families(self) -> list[str]:
        return sorted({family for family, _driver in self._definitions})

    def to_dict(self) -> dict[str, Any]:
        return {
            "families": {
                family: [definition.to_dict() for definition in self.definitions_for_family(family)]
                for family in self.families()
            }
        }


def attach_storage_driver(component: Any, selection: StorageDriverSelection) -> Any:
    setattr(component, "__voicebot_storage_driver__", selection)
    return component


def attached_storage_driver(component: Any) -> StorageDriverSelection | None:
    value = getattr(component, "__voicebot_storage_driver__", None)
    return value if isinstance(value, StorageDriverSelection) else None


def normalize_driver_name(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "inmemory": "memory",
        "in_memory": "memory",
        "jsonl": "jsonl",
        "json": "json",
        "fs": "filesystem",
        "file": "filesystem",
    }
    return aliases.get(normalized, normalized)


def redacted_options(options: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in options.items():
        lowered = key.lower()
        if any(marker in lowered for marker in ("password", "secret", "token", "api_key", "credential", "url")):
            result[key] = {"configured": bool(value), "redacted": True}
        else:
            result[key] = value
    return result
