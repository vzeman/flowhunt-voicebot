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

    def read(self, call_id: str) -> list[dict]:
        path = self.directory / f"{safe_name(call_id)}.jsonl"
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


def safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)
