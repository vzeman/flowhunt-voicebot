from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import threading

from ..sip_trunks import SipTrunk, render_pjsip_trunks, trunk_from_dict, validate_trunk, validate_trunk_id


SCHEMA_VERSION = 1


class SQLiteSipTrunkStore:
    def __init__(self, database_url: str, pjsip_include_path: str) -> None:
        self.database_url = database_url
        self.path = _sqlite_path(database_url)
        self.pjsip_include_path = Path(pjsip_include_path)
        self._db_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pjsip_include_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self.load_diagnostics: dict[str, int] = {
            "loaded_trunks": 0,
            "skipped_invalid_trunks": 0,
            "schema_version": SCHEMA_VERSION,
        }
        self._migrate()
        if not self.pjsip_include_path.exists():
            self.render()

    def list(self) -> list[SipTrunk]:
        with self._db_lock:
            rows = self._connection.execute(
                """
                SELECT trunk_json
                FROM sip_trunks
                ORDER BY trunk_id ASC
                """
            ).fetchall()
        trunks: list[SipTrunk] = []
        for row in rows:
            try:
                trunks.append(_trunk_from_row(row))
                self.load_diagnostics["loaded_trunks"] += 1
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                self.load_diagnostics["skipped_invalid_trunks"] += 1
        return trunks

    def get(self, trunk_id: str) -> SipTrunk | None:
        validate_trunk_id(trunk_id)
        with self._db_lock:
            row = self._connection.execute(
                """
                SELECT trunk_json
                FROM sip_trunks
                WHERE trunk_id = ?
                """,
                (trunk_id,),
            ).fetchone()
        return _trunk_from_row(row) if row is not None else None

    def upsert(self, trunk: SipTrunk) -> SipTrunk:
        validate_trunk(trunk)
        payload = json.dumps(trunk.stored_dict(), sort_keys=True)
        with self._db_lock:
            self._connection.execute(
                """
                INSERT INTO sip_trunks
                    (trunk_id, host, user, enabled, trunk_json, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(trunk_id)
                DO UPDATE SET
                    host = excluded.host,
                    user = excluded.user,
                    enabled = excluded.enabled,
                    trunk_json = excluded.trunk_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (trunk.trunk_id, trunk.host, trunk.user, int(trunk.enabled), payload),
            )
            self._connection.commit()
        self.render()
        return trunk

    def set_enabled(self, trunk_id: str, enabled: bool) -> SipTrunk | None:
        trunk = self.get(trunk_id)
        if trunk is None:
            return None
        updated = SipTrunk(**{**trunk.stored_dict(), "enabled": enabled})
        return self.upsert(updated)

    def delete(self, trunk_id: str) -> SipTrunk | None:
        validate_trunk_id(trunk_id)
        removed = self.get(trunk_id)
        if removed is None:
            return None
        with self._db_lock:
            self._connection.execute("DELETE FROM sip_trunks WHERE trunk_id = ?", (trunk_id,))
            self._connection.commit()
        self.render()
        return removed

    def render(self) -> None:
        trunks = [trunk for trunk in self.list() if trunk.enabled]
        self.pjsip_include_path.write_text(render_pjsip_trunks(trunks), encoding="utf-8")

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
                CREATE TABLE IF NOT EXISTS sip_trunks (
                    trunk_id TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    user TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    trunk_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._connection.execute("CREATE INDEX IF NOT EXISTS idx_sip_trunks_enabled ON sip_trunks(enabled)")
            self._connection.execute(
                "INSERT OR IGNORE INTO voicebot_schema_migrations(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self._connection.commit()


def _sqlite_path(database_url: str) -> Path:
    if database_url.startswith("sqlite:///"):
        return Path(database_url.removeprefix("sqlite:///"))
    if database_url.startswith("sqlite://"):
        return Path(database_url.removeprefix("sqlite://"))
    return Path(database_url)


def _trunk_from_row(row: sqlite3.Row) -> SipTrunk:
    payload = json.loads(str(row["trunk_json"]))
    if not isinstance(payload, dict):
        raise ValueError("sip trunk row must contain a JSON object")
    return trunk_from_dict(payload)
