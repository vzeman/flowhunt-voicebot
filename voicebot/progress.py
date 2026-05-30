from __future__ import annotations

from dataclasses import dataclass
import re
import time


PROVIDER_STATUS_PATTERN = re.compile(r"\b(?:TaskStatus|IssueStatus|FlowStatus)\.([A-Z_]+)\b")
STATUS_ONLY_PATTERN = re.compile(r"[A-Za-z]+Status\.[A-Z_]+")


@dataclass
class ProgressMemoryEntry:
    message: str
    spoken_at: float


class ProgressCadenceMemory:
    def __init__(self, default_interval_seconds: float) -> None:
        self.default_interval_seconds = max(1.0, float(default_interval_seconds))
        self._entries: dict[str, ProgressMemoryEntry] = {}

    def should_speak(
        self,
        key: str,
        message: str,
        *,
        now: float | None = None,
        min_interval_seconds: float | None = None,
    ) -> bool:
        normalized = normalize_progress_message(message)
        current = time.monotonic() if now is None else now
        interval = self.default_interval_seconds if min_interval_seconds is None else max(1.0, min_interval_seconds)
        previous = self._entries.get(key)
        if previous is not None and previous.message == normalized:
            return False
        if previous is not None and current - previous.spoken_at < interval:
            return False
        self._entries[key] = ProgressMemoryEntry(normalized, current)
        return True


def normalize_progress_message(message: str) -> str:
    original = message.strip()
    text = PROVIDER_STATUS_PATTERN.sub(lambda match: humanize_status(match.group(1)), message)
    text = re.sub(r"\b[A-Za-z]+Status\.([A-Z_]+)\b", lambda match: humanize_status(match.group(1)), text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "The colleague is still working on it."
    lowered = text.lower()
    if lowered in {"retry", "pending", "running", "processing", "waiting for worker", "waiting_for_worker"}:
        return "The colleague is still working on it."
    if lowered == "still working" and STATUS_ONLY_PATTERN.fullmatch(original):
        return "The colleague is still working on it."
    return text


def humanize_status(value: str) -> str:
    normalized = value.strip().lower().replace("_", " ")
    if normalized in {"retry", "pending", "running", "processing", "waiting for worker"}:
        return "still working"
    if normalized in {"success", "completed", "done"}:
        return "completed"
    if normalized in {"failed", "error"}:
        return "failed"
    return normalized or "still working"
