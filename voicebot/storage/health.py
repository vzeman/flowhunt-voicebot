from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any

from .drivers import StoreHealth, attached_storage_driver


def storage_component_health(component: Any) -> StoreHealth:
    diagnostics = storage_component_diagnostics(component)
    if diagnostics.get("writable") is False:
        return StoreHealth(
            False,
            "storage path is not writable",
            diagnostics,
        )
    if diagnostics["warning_count"] > 0:
        return StoreHealth(
            True,
            "storage is reachable with recovery warnings",
            diagnostics,
        )
    return StoreHealth(True, "storage is reachable", diagnostics)


def storage_component_diagnostics(component: Any) -> dict[str, Any]:
    path = getattr(component, "path", None)
    diagnostics = dict(getattr(component, "load_diagnostics", {}) or {})
    driver = attached_storage_driver(component)
    details: dict[str, Any] = {
        "kind": component.__class__.__name__,
        "path": str(path) if path is not None else None,
        "driver": driver.to_dict() if driver is not None else None,
        "load_diagnostics": diagnostics,
        "warning_count": recovery_warning_count(diagnostics),
        "snapshot": compact_snapshot(component_snapshot(component)),
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
            return snapshot
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
    if "leases" in snapshot:
        compact["lease_count"] = len(snapshot.get("leases") or [])
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
