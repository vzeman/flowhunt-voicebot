from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import json
import tempfile
import threading


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    path: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "metadata": self.metadata,
        }


class FilesystemArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.path = Path(root)
        self.path.mkdir(parents=True, exist_ok=True)
        self.load_diagnostics: dict[str, int] = {
            "loaded_artifacts": 0,
            "skipped_malformed_metadata": 0,
        }
        self._lock = threading.Lock()

    def put(self, artifact_id: str, data: bytes, metadata: dict[str, Any] | None = None) -> ArtifactRecord:
        path = self._artifact_path(artifact_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".artifact-", delete=False) as handle:
                tmp_path = Path(handle.name)
                handle.write(data)
                handle.flush()
            tmp_path.replace(path)
            self._write_metadata(path, metadata or {})
        return ArtifactRecord(artifact_id=artifact_id, path=str(path), metadata=metadata or {})

    def get(self, artifact_id: str) -> bytes | None:
        path = self._artifact_path(artifact_id)
        try:
            with self._lock:
                if not path.exists():
                    return None
                return path.read_bytes()
        except OSError:
            return None

    def delete(self, artifact_id: str) -> bool:
        path = self._artifact_path(artifact_id)
        deleted = False
        with self._lock:
            for candidate in (path, self._metadata_path(path)):
                try:
                    candidate.unlink()
                    deleted = True
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
        return deleted

    def list(self) -> list[ArtifactRecord]:
        records: list[ArtifactRecord] = []
        with self._lock:
            for path in sorted(self.path.glob("*")):
                if not path.is_file() or path.name.endswith(".metadata.json") or path.name.startswith("."):
                    continue
                metadata = self._read_metadata(path)
                records.append(ArtifactRecord(artifact_id=path.name, path=str(path), metadata=metadata))
        return records

    def _artifact_path(self, artifact_id: str) -> Path:
        safe_id = safe_artifact_id(artifact_id)
        return self.path / safe_id

    def _metadata_path(self, path: Path) -> Path:
        return path.with_name(f"{path.name}.metadata.json")

    def _write_metadata(self, path: Path, metadata: dict[str, Any]) -> None:
        metadata_path = self._metadata_path(path)
        tmp = metadata_path.with_suffix(metadata_path.suffix + ".tmp")
        tmp.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(metadata_path)

    def _read_metadata(self, path: Path) -> dict[str, Any]:
        metadata_path = self._metadata_path(path)
        if not metadata_path.exists():
            return {}
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.load_diagnostics["skipped_malformed_metadata"] += 1
            return {}
        return payload if isinstance(payload, dict) else {}


def safe_artifact_id(value: str) -> str:
    text = value.strip().replace("\\", "_").replace("/", "_")
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in text) or "artifact"
