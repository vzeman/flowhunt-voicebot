from __future__ import annotations

from collections import defaultdict
from typing import Any

from .events import VoicebotEvent
from .observability import provider_observability_summary


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
                "p50": percentile(values, 50),
                "p90": percentile(values, 90),
                "latest": latest[name],
            }
            for name, values in sorted(grouped.items())
        },
        "providers": provider_observability_summary(events)["providers"],
    }


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        raise ValueError("values must not be empty")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (percentile_value / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
