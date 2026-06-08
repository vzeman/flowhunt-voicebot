from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import sqlite3
import threading

from ..transcripts import event_id


SCHEMA_VERSION = 1


class SQLiteTranscriptStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.path = _sqlite_path(database_url)
        self._db_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self.load_diagnostics: dict[str, int] = {
            "schema_version": SCHEMA_VERSION,
            "skipped_invalid_rows": 0,
        }
        self._migrate()

    def append(self, event) -> None:
        if event.call_id == "system":
            return
        payload = {
            "id": event.id,
            "call_id": event.call_id,
            "type": event.type,
            "timestamp": event.timestamp,
            "data": event.data,
        }
        with self._db_lock:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO voicebot_transcript_events
                    (event_id, call_id, type, timestamp, data_json, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(event.id),
                    str(event.call_id),
                    str(event.type),
                    str(event.timestamp),
                    json.dumps(event.data, ensure_ascii=False, sort_keys=True),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._connection.commit()

    def read(self, call_id: str, after: int = 0, limit: int | None = None) -> list[dict]:
        args: list[Any] = [call_id, after]
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            args.append(limit)
        with self._db_lock:
            rows = self._connection.execute(
                f"""
                SELECT payload_json
                FROM voicebot_transcript_events
                WHERE call_id = ? AND event_id > ?
                ORDER BY event_id ASC
                {limit_clause}
                """,
                args,
            ).fetchall()
        return [payload for row in rows if (payload := _payload_from_row(row, self.load_diagnostics)) is not None]

    def list_call_ids(self) -> list[str]:
        with self._db_lock:
            rows = self._connection.execute(
                """
                SELECT call_id
                FROM voicebot_transcript_events
                GROUP BY call_id
                ORDER BY call_id ASC
                """
            ).fetchall()
        return [str(row["call_id"]) for row in rows]

    def summaries(self, after_call_id: str | None = None, limit: int | None = None) -> list[dict]:
        args: list[Any] = []
        where = ""
        if after_call_id:
            where = "WHERE call_id > ?"
            args.append(after_call_id)
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            args.append(limit)
        with self._db_lock:
            rows = self._connection.execute(
                f"""
                SELECT
                    call_id,
                    COUNT(*) AS event_count,
                    MIN(event_id) AS first_event_id,
                    MAX(event_id) AS last_event_id,
                    MIN(timestamp) AS first_timestamp,
                    MAX(timestamp) AS last_timestamp
                FROM voicebot_transcript_events
                {where}
                GROUP BY call_id
                ORDER BY call_id ASC
                {limit_clause}
                """,
                args,
            ).fetchall()
        return [
            {
                "call_id": row["call_id"],
                "event_count": row["event_count"],
                "first_event_id": row["first_event_id"],
                "last_event_id": row["last_event_id"],
                "first_timestamp": row["first_timestamp"],
                "last_timestamp": row["last_timestamp"],
                "skipped_line_count": 0,
            }
            for row in rows
        ]

    def stats(self, after_call_id: str | None = None, limit: int | None = None) -> dict:
        summaries = self.summaries(after_call_id=after_call_id, limit=limit)
        return {
            "transcript_count": len(summaries),
            "event_count": sum(int(summary["event_count"]) for summary in summaries),
            "skipped_line_count": 0,
            "corrupt_transcript_count": 0,
            "corrupt_call_ids": [],
        }

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
                CREATE TABLE IF NOT EXISTS voicebot_transcript_events (
                    event_id INTEGER PRIMARY KEY,
                    call_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_voicebot_transcript_events_call_event ON voicebot_transcript_events(call_id, event_id)"
            )
            self._connection.execute("INSERT OR IGNORE INTO voicebot_schema_migrations(version) VALUES (?)", (SCHEMA_VERSION,))
            self._connection.commit()


def _sqlite_path(database_url: str) -> Path:
    if database_url.startswith("sqlite:///"):
        return Path(database_url.removeprefix("sqlite:///"))
    if database_url.startswith("sqlite://"):
        return Path(database_url.removeprefix("sqlite://"))
    return Path(database_url)


def _payload_from_row(row: sqlite3.Row, diagnostics: dict[str, int]) -> dict | None:
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        diagnostics["skipped_invalid_rows"] += 1
        return None
    if not isinstance(payload, dict) or event_id(payload) < 1:
        diagnostics["skipped_invalid_rows"] += 1
        return None
    return payload
