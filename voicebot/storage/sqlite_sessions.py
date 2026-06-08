from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import sqlite3
import threading

from ..workspace_model import VoicebotSessionRecord, VoicebotSessionStore, voicebot_session_from_dict


SCHEMA_VERSION = 1


class SQLiteVoicebotSessionStore(VoicebotSessionStore):
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.path = _sqlite_path(database_url)
        self._db_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self.load_diagnostics: dict[str, int] = {
            "loaded_sessions": 0,
            "skipped_invalid_sessions": 0,
            "schema_version": SCHEMA_VERSION,
        }
        self._migrate()

    def save(self, session: VoicebotSessionRecord) -> VoicebotSessionRecord:
        existing = self.get(session.session_id)
        if existing is not None and existing.workspace_id != session.workspace_id:
            raise ValueError("cannot move voicebot session across workspaces")
        if existing is not None and existing.voicebot_id != session.voicebot_id:
            raise ValueError("cannot move voicebot session across voicebots")
        payload = json.dumps(session.as_dict(), sort_keys=True)
        with self._db_lock:
            self._connection.execute(
                """
                INSERT INTO voicebot_sessions
                    (session_id, workspace_id, voicebot_id, status, started_at, ended_at, session_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_id)
                DO UPDATE SET
                    status = excluded.status,
                    ended_at = excluded.ended_at,
                    session_json = excluded.session_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    session.session_id,
                    session.workspace_id,
                    session.voicebot_id,
                    session.status,
                    session.started_at,
                    session.ended_at,
                    payload,
                ),
            )
            self._connection.commit()
        return session

    def get(self, session_id: str, workspace_id: str | None = None) -> VoicebotSessionRecord | None:
        clauses = ["session_id = ?"]
        args: list[Any] = [session_id]
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            args.append(workspace_id)
        with self._db_lock:
            row = self._connection.execute(
                f"""
                SELECT session_json
                FROM voicebot_sessions
                WHERE {' AND '.join(clauses)}
                """,
                args,
            ).fetchone()
        return _session_from_row(row) if row is not None else None

    def list(
        self,
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
        active_only: bool = False,
    ) -> tuple[VoicebotSessionRecord, ...]:
        clauses: list[str] = []
        args: list[Any] = []
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            args.append(workspace_id)
        if voicebot_id is not None:
            clauses.append("voicebot_id = ?")
            args.append(voicebot_id)
        if active_only:
            clauses.append("status = 'active'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._db_lock:
            rows = self._connection.execute(
                f"""
                SELECT session_json
                FROM voicebot_sessions
                {where}
                ORDER BY session_id ASC
                """,
                args,
            ).fetchall()
        sessions: list[VoicebotSessionRecord] = []
        for row in rows:
            try:
                sessions.append(_session_from_row(row))
                self.load_diagnostics["loaded_sessions"] += 1
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                self.load_diagnostics["skipped_invalid_sessions"] += 1
        return tuple(sessions)

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
                CREATE TABLE IF NOT EXISTS voicebot_sessions (
                    session_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    voicebot_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    session_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_voicebot_sessions_scope ON voicebot_sessions(workspace_id, voicebot_id, status)"
            )
            self._connection.execute("INSERT OR IGNORE INTO voicebot_schema_migrations(version) VALUES (?)", (SCHEMA_VERSION,))
            self._connection.commit()


def _sqlite_path(database_url: str) -> Path:
    if database_url.startswith("sqlite:///"):
        return Path(database_url.removeprefix("sqlite:///"))
    if database_url.startswith("sqlite://"):
        return Path(database_url.removeprefix("sqlite://"))
    return Path(database_url)


def _session_from_row(row: sqlite3.Row) -> VoicebotSessionRecord:
    return voicebot_session_from_dict(json.loads(str(row["session_json"])))
