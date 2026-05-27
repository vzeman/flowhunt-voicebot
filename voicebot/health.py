from __future__ import annotations

from dataclasses import dataclass
import tempfile
from typing import Any

from .asterisk_control import AsteriskAMI
from .event_catalog import missing_catalog_event_types
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
) -> dict[str, Any]:
    checks = {
        "transcripts": transcript_store_check(transcripts).to_dict(),
        "ami": ami_configuration_check(asterisk).to_dict(),
        "providers": provider_catalog_check().to_dict(),
        "event_catalog": event_catalog_check().to_dict(),
    }
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
        stats = transcripts.stats()
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
    missing = sorted(missing_catalog_event_types())
    return HealthCheck(
        not missing,
        "event catalog covers all declared event types" if not missing else "event catalog is missing event types",
        {"missing_event_types": missing},
    )
