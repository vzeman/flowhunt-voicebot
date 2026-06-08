from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import sqlite3
import threading

from ..events import EventStore, EventType, VoicebotEvent, event_from_dict, utc_now
from ..transcripts import TranscriptStore


SCHEMA_VERSION = 1


class SQLiteEventStore(EventStore):
    def __init__(
        self,
        database_url: str,
        max_context_events: int,
        transcript_store: TranscriptStore | None = None,
    ) -> None:
        self.database_url = database_url
        self.path = _sqlite_path(database_url)
        self._db_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self.load_diagnostics: dict[str, int] = {
            "loaded_events": 0,
            "skipped_invalid_events": 0,
            "schema_version": SCHEMA_VERSION,
        }
        self._migrate()
        super().__init__(
            max_context_events=max_context_events,
            transcript_store=transcript_store,
            initial_events=self._load_events_for_context(max_context_events),
        )

    def append(self, call_id: str, event_type: EventType, data: dict[str, Any] | None = None) -> VoicebotEvent:
        with self._db_lock:
            timestamp = utc_now()
            payload = data or {}
            workspace_id = _optional_text(payload.get("workspace_id"))
            voicebot_id = _optional_text(payload.get("voicebot_id"))
            session_id = _optional_text(payload.get("session_id", call_id))
            cursor = self._connection.execute(
                """
                INSERT INTO voicebot_events
                    (call_id, type, timestamp, workspace_id, voicebot_id, session_id, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (call_id, event_type, timestamp, workspace_id, voicebot_id, session_id, json.dumps(payload, sort_keys=True)),
            )
            self._connection.commit()
            event = VoicebotEvent(int(cursor.lastrowid), call_id, event_type, timestamp, payload)
        with self._lock:
            self._events.append(event)
            if self._transcript_store is not None:
                self._transcript_store.append(event)
            self._compact_locked()
        return event

    def event_id_strategy(self) -> dict[str, Any]:
        return {
            "name": "sqlite_autoincrement",
            "scope": "node_database",
            "monotonic": True,
            "collision_safe_across_processes": True,
            "collision_safe_across_nodes": False,
        }

    def list_events(
        self,
        after: int = 0,
        call_id: str | None = None,
        limit: int = 200,
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
        session_id: str | None = None,
    ) -> list[VoicebotEvent]:
        clauses = ["id > ?"]
        args: list[Any] = [after]
        if call_id is not None:
            clauses.append("call_id = ?")
            args.append(call_id)
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            args.append(workspace_id)
        if voicebot_id is not None:
            clauses.append("voicebot_id = ?")
            args.append(voicebot_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            args.append(session_id)
        args.append(limit)
        with self._db_lock:
            rows = self._connection.execute(
                f"""
                SELECT id, call_id, type, timestamp, data_json
                FROM voicebot_events
                WHERE {' AND '.join(clauses)}
                ORDER BY id ASC
                LIMIT ?
                """,
                args,
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def get_event(self, event_id: int) -> VoicebotEvent | None:
        with self._db_lock:
            row = self._connection.execute(
                "SELECT id, call_id, type, timestamp, data_json FROM voicebot_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        return _event_from_row(row) if row is not None else None

    def close(self) -> None:
        with self._db_lock:
            self._connection.close()

    def _migrate(self) -> None:
        with self._db_lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS voicebot_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS voicebot_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    workspace_id TEXT,
                    voicebot_id TEXT,
                    session_id TEXT,
                    data_json TEXT NOT NULL
                )
                """
            )
            self._connection.execute("CREATE INDEX IF NOT EXISTS idx_voicebot_events_call_id_id ON voicebot_events(call_id, id)")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_voicebot_events_scope_id ON voicebot_events(workspace_id, voicebot_id, session_id, id)"
            )
            self._connection.execute("INSERT OR IGNORE INTO voicebot_schema_migrations(version) VALUES (?)", (SCHEMA_VERSION,))
            self._connection.commit()

    def _load_events_for_context(self, max_context_events: int) -> list[VoicebotEvent]:
        with self._db_lock:
            rows = self._connection.execute(
                """
                SELECT id, call_id, type, timestamp, data_json
                FROM voicebot_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (max_context_events,),
            ).fetchall()
        events = [_event_from_row(row) for row in reversed(rows)]
        self.load_diagnostics["loaded_events"] = len(events)
        return events


def _sqlite_path(database_url: str) -> Path:
    if database_url.startswith("sqlite:///"):
        return Path(database_url.removeprefix("sqlite:///"))
    if database_url.startswith("sqlite://"):
        return Path(database_url.removeprefix("sqlite://"))
    return Path(database_url)


def _event_from_row(row: sqlite3.Row) -> VoicebotEvent:
    data = json.loads(str(row["data_json"]))
    event = event_from_dict(
        {
            "id": row["id"],
            "call_id": row["call_id"],
            "type": row["type"],
            "timestamp": row["timestamp"],
            "data": data,
        }
    )
    if event is None:
        raise ValueError("invalid event row")
    return event


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
