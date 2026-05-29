from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Any

from .asterisk_control import AsteriskAMI
from .event_catalog import event_catalog_integrity_issues, missing_catalog_event_types
from .provider_catalog import provider_catalog
from .transcripts import TranscriptStore


@dataclass(frozen=True)
class HealthCheck:
    ok: bool
    message: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            **self.details,
        }


def readiness_report(
    *,
    transcripts: TranscriptStore,
    asterisk: AsteriskAMI | None,
    active_call_ids: list[str],
    storage_components: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks = {
        "transcripts": transcript_store_check(transcripts).to_dict(),
        "ami": ami_configuration_check(asterisk).to_dict(),
        "providers": provider_catalog_check().to_dict(),
        "event_catalog": event_catalog_check().to_dict(),
    }
    if storage_components is not None:
        checks["durable_storage"] = durable_storage_check(storage_components).to_dict()
    return {
        "ok": all(check["ok"] for check in checks.values()),
        "active_calls": active_call_ids,
        "checks": checks,
    }


def transcript_store_check(transcripts: TranscriptStore) -> HealthCheck:
    directory = transcripts.directory
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".health-", dir=directory, delete=True) as handle:
            handle.write(b"ok")
            handle.flush()
        stats = transcripts.stats(limit=None)
        message = "transcript directory is writable"
        if stats["corrupt_transcript_count"]:
            message = "transcript directory is writable with corrupt transcript rows"
        return HealthCheck(True, message, {"path": str(directory), **stats})
    except OSError as exc:
        return HealthCheck(False, "transcript directory is not writable", {"path": str(directory), "error": str(exc)})


def ami_configuration_check(asterisk: AsteriskAMI | None) -> HealthCheck:
    if asterisk is None:
        return HealthCheck(True, "AMI control is not configured", {"configured": False})
    return HealthCheck(
        True,
        "AMI control is configured",
        {
            "configured": True,
            "host": asterisk.host,
            "port": asterisk.port,
            "username": asterisk.username,
        },
    )


def provider_catalog_check() -> HealthCheck:
    catalog = provider_catalog()
    details = {
        provider_type: sorted(values.get("supported", []))
        for provider_type, values in catalog.items()
    }
    missing = [provider_type for provider_type, supported in details.items() if not supported]
    return HealthCheck(
        not missing,
        "provider catalog is populated" if not missing else "provider catalog has empty provider groups",
        {"supported": details, "empty_groups": missing},
    )


def event_catalog_check() -> HealthCheck:
    issues = event_catalog_integrity_issues()
    missing = sorted(missing_catalog_event_types())
    return HealthCheck(
        not issues,
        "event catalog is valid" if not issues else "event catalog has integrity issues",
        {"missing_event_types": missing, "integrity_issues": issues},
    )


def durable_storage_check(components: dict[str, Any]) -> HealthCheck:
    stores = {name: store_diagnostics(component) for name, component in sorted(components.items())}
    unwritable = [
        {"name": name, "path": details["path"], "error": details["writable_error"]}
        for name, details in stores.items()
        if details.get("writable") is False
    ]
    warning_counts = {
        name: details["warning_count"]
        for name, details in stores.items()
        if details["warning_count"] > 0
    }
    if unwritable:
        return HealthCheck(
            False,
            "durable storage has unwritable paths",
            {"stores": stores, "unwritable": unwritable, "warning_counts": warning_counts},
        )
    message = "durable storage is reachable"
    if warning_counts:
        message = "durable storage is reachable with recovery warnings"
    return HealthCheck(True, message, {"stores": stores, "unwritable": [], "warning_counts": warning_counts})


def store_diagnostics(component: Any) -> dict[str, Any]:
    path = getattr(component, "path", None)
    diagnostics = dict(getattr(component, "load_diagnostics", {}) or {})
    snapshot = component_snapshot(component)
    details: dict[str, Any] = {
        "kind": component.__class__.__name__,
        "path": str(path) if path is not None else None,
        "load_diagnostics": diagnostics,
        "warning_count": recovery_warning_count(diagnostics),
        "snapshot": snapshot,
    }
    if path is not None:
        writable, error = path_is_writable(Path(path))
        details["writable"] = writable
        if error:
            details["writable_error"] = error
    return details


def recovery_warning_count(diagnostics: dict[str, Any]) -> int:
    count = 0
    for name, value in diagnostics.items():
        if name.startswith("skipped_") or name.startswith("requeued_"):
            try:
                count += int(value)
            except (TypeError, ValueError):
                continue
    return count


def component_snapshot(component: Any) -> dict[str, Any]:
    if hasattr(component, "snapshot"):
        snapshot = component.snapshot()
        if isinstance(snapshot, dict):
            return compact_snapshot(snapshot)
    if hasattr(component, "list"):
        try:
            items = component.list()
        except TypeError:
            return {}
        return {"count": len(items)}
    return {}


def compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    if "responded_event_ids" in snapshot:
        compact["responded_event_count"] = len(snapshot.get("responded_event_ids") or [])
    if "claims" in snapshot:
        compact["claim_count"] = len(snapshot.get("claims") or {})
    if "pending" in snapshot:
        compact["pending_count"] = sum(len(items) for items in (snapshot.get("pending") or {}).values())
    if "claimed" in snapshot:
        compact["claimed_count"] = len(snapshot.get("claimed") or [])
    return compact or snapshot


def path_is_writable(path: Path) -> tuple[bool, str | None]:
    directory = path.parent if path.suffix else path
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".health-", dir=directory, delete=True) as handle:
            handle.write(b"ok")
            handle.flush()
        return True, None
    except OSError as exc:
        return False, str(exc)
