from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
import threading
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class CallStateStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}

    def upsert(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        call_id = _required_str(snapshot, "call_id")
        with self._lock:
            state = {
                **deepcopy(snapshot),
                "call_id": call_id,
                "state": "active",
                "updated_at": utc_now_iso(),
            }
            self._states[call_id] = state
            return deepcopy(state)

    def end(self, call_id: str) -> dict[str, Any] | None:
        normalized = call_id.strip()
        if not normalized:
            raise ValueError("call_id is required")
        with self._lock:
            state = self._states.get(normalized)
            if state is None:
                return None
            ended = {**deepcopy(state), "state": "ended", "ended_at": utc_now_iso(), "updated_at": utc_now_iso()}
            self._states[normalized] = ended
            return deepcopy(ended)

    def get(self, call_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._states.get(call_id)
            return deepcopy(state) if state is not None else None

    def list(self, active_only: bool = False) -> tuple[dict[str, Any], ...]:
        with self._lock:
            states = [
                deepcopy(state)
                for state in self._states.values()
                if not active_only or state.get("state") == "active"
            ]
        return tuple(sorted(states, key=lambda item: str(item["call_id"])))


class JsonCallStateStore(CallStateStore):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.load_diagnostics: dict[str, int] = {
            "loaded_states": 0,
            "skipped_malformed_json": 0,
            "skipped_invalid_states": 0,
            "skipped_duplicate_call_ids": 0,
        }
        super().__init__()
        self._load()

    def upsert(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        state = super().upsert(snapshot)
        self._save()
        return state

    def end(self, call_id: str) -> dict[str, Any] | None:
        state = super().end(call_id)
        if state is not None:
            self._save()
        return state

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.load_diagnostics["skipped_malformed_json"] += 1
            return
        seen: set[str] = set()
        for item in payload.get("calls", []):
            try:
                call_id = _required_str(item, "call_id")
                state = dict(item)
            except (TypeError, ValueError):
                self.load_diagnostics["skipped_invalid_states"] += 1
                continue
            if call_id in seen:
                self.load_diagnostics["skipped_duplicate_call_ids"] += 1
                continue
            seen.add(call_id)
            self._states[call_id] = state
            self.load_diagnostics["loaded_states"] += 1

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "calls": list(self.list())}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def _required_str(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if value is None:
        raise ValueError(f"{field} is required")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text
