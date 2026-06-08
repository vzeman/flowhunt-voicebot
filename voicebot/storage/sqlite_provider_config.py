from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import sqlite3
import threading

from ..provider_config import (
    ProviderConfigStore,
    VoicebotProviderConfig,
    provider_config_from_dict,
    provider_config_to_dict,
)


SCHEMA_VERSION = 1


class SQLiteProviderConfigStore(ProviderConfigStore):
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.path = _sqlite_path(database_url)
        self._db_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self.load_diagnostics: dict[str, int] = {
            "loaded_configs": 0,
            "skipped_invalid_configs": 0,
            "schema_version": SCHEMA_VERSION,
        }
        self._migrate()

    def save(self, config: VoicebotProviderConfig) -> VoicebotProviderConfig:
        payload = json.dumps(provider_config_to_dict(config), sort_keys=True)
        with self._db_lock:
            self._connection.execute(
                """
                INSERT INTO voicebot_provider_configs (workspace_id, voicebot_id, config_json, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(workspace_id, voicebot_id)
                DO UPDATE SET config_json = excluded.config_json, updated_at = CURRENT_TIMESTAMP
                """,
                (config.workspace_id, config.voicebot_id, payload),
            )
            self._connection.commit()
        return config

    def get(self, workspace_id: str, voicebot_id: str) -> VoicebotProviderConfig | None:
        with self._db_lock:
            row = self._connection.execute(
                """
                SELECT config_json
                FROM voicebot_provider_configs
                WHERE workspace_id = ? AND voicebot_id = ?
                """,
                (workspace_id, voicebot_id),
            ).fetchone()
        return _config_from_row(row) if row is not None else None

    def list(self, workspace_id: str | None = None) -> list[VoicebotProviderConfig]:
        args: list[Any] = []
        where = ""
        if workspace_id is not None:
            where = "WHERE workspace_id = ?"
            args.append(workspace_id)
        with self._db_lock:
            rows = self._connection.execute(
                f"""
                SELECT config_json
                FROM voicebot_provider_configs
                {where}
                ORDER BY workspace_id ASC, voicebot_id ASC
                """,
                args,
            ).fetchall()
        configs: list[VoicebotProviderConfig] = []
        for row in rows:
            try:
                configs.append(_config_from_row(row))
                self.load_diagnostics["loaded_configs"] += 1
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                self.load_diagnostics["skipped_invalid_configs"] += 1
        return configs

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
                CREATE TABLE IF NOT EXISTS voicebot_provider_configs (
                    workspace_id TEXT NOT NULL,
                    voicebot_id TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (workspace_id, voicebot_id)
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_voicebot_provider_configs_workspace ON voicebot_provider_configs(workspace_id)"
            )
            self._connection.execute("INSERT OR IGNORE INTO voicebot_schema_migrations(version) VALUES (?)", (SCHEMA_VERSION,))
            self._connection.commit()


def _sqlite_path(database_url: str) -> Path:
    if database_url.startswith("sqlite:///"):
        return Path(database_url.removeprefix("sqlite:///"))
    if database_url.startswith("sqlite://"):
        return Path(database_url.removeprefix("sqlite://"))
    return Path(database_url)


def _config_from_row(row: sqlite3.Row) -> VoicebotProviderConfig:
    return provider_config_from_dict(json.loads(str(row["config_json"])))
