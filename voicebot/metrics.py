from __future__ import annotations

from collections import defaultdict
from typing import Any

from .events import VoicebotEvent


def summarize_metrics(events: list[VoicebotEvent]) -> dict[str, Any]:
    grouped: dict[str, list[float]] = defaultdict(list)
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.type != "metrics":
            continue
        name = event.data.get("name")
        value = event.data.get("value")
        if not isinstance(name, str):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        grouped[name].append(number)
        latest[name] = {"value": number, "timestamp": event.timestamp, "event_id": event.id}

    return {
        "metrics": {
            name: {
                "count": len(values),
                "min": min(values),
                "max": max(values),
                "avg": sum(values) / len(values),
                "latest": latest[name],
            }
            for name, values in sorted(grouped.items())
        }
    }
