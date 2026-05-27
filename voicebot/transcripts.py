from __future__ import annotations

from pathlib import Path
import json
import threading


class TranscriptStore:
    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, event) -> None:
        if event.call_id == "system":
            return
        path = self.directory / f"{safe_name(event.call_id)}.jsonl"
        payload = {
            "id": event.id,
            "call_id": event.call_id,
            "type": event.type,
            "timestamp": event.timestamp,
            "data": event.data,
        }
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def read(self, call_id: str, after: int = 0, limit: int | None = None) -> list[dict]:
        path = self.directory / f"{safe_name(call_id)}.jsonl"
        events = [event for event in self._read_path(path) if int(event.get("id") or 0) > after]
        if limit is not None:
            events = events[:limit]
        return events

    def list_call_ids(self) -> list[str]:
        with self._lock:
            return sorted(path.stem for path in self.directory.glob("*.jsonl") if path.is_file())

    def summaries(self, after_call_id: str | None = None, limit: int | None = None) -> list[dict]:
        result = []
        with self._lock:
            paths = sorted(path for path in self.directory.glob("*.jsonl") if path.is_file())
        if after_call_id:
            safe_after = safe_name(after_call_id)
            paths = [path for path in paths if path.stem > safe_after]
        if limit is not None:
            paths = paths[:limit]
        for path in paths:
            events = self._read_path(path)
            if not events:
                continue
            result.append(
                {
                    "call_id": events[0].get("call_id", path.stem),
                    "event_count": len(events),
                    "first_event_id": events[0].get("id"),
                    "last_event_id": events[-1].get("id"),
                    "first_timestamp": events[0].get("timestamp"),
                    "last_timestamp": events[-1].get("timestamp"),
                }
            )
        return result

    def _read_path(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


def safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)
