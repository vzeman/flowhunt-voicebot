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


class S3ArtifactStore:
    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str = "",
        region_name: str = "",
        prefix: str = "voicebot/audio-artifacts",
        client: Any | None = None,
    ) -> None:
        if not bucket.strip():
            raise ValueError("S3 artifact store requires VOICEBOT_OBJECT_STORAGE_BUCKET")
        self.bucket = bucket.strip()
        self.endpoint_url = endpoint_url.strip()
        self.region_name = region_name.strip()
        self.prefix = prefix.strip().strip("/")
        self.client = client or _s3_client(endpoint_url=self.endpoint_url, region_name=self.region_name)
        self.load_diagnostics: dict[str, int] = {
            "loaded_artifacts": 0,
            "skipped_malformed_metadata": 0,
        }

    def put(self, artifact_id: str, data: bytes, metadata: dict[str, Any] | None = None) -> ArtifactRecord:
        key = self._key(artifact_id)
        encoded_metadata = self._encode_metadata(metadata or {})
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, Metadata=encoded_metadata)
        return ArtifactRecord(artifact_id=artifact_id, path=self._uri(key), metadata=metadata or {})

    def get(self, artifact_id: str) -> bytes | None:
        key = self._key(artifact_id)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # pragma: no cover - concrete SDK exceptions vary by client
            if _is_not_found_error(exc):
                return None
            raise
        body = response.get("Body")
        if hasattr(body, "read"):
            return body.read()
        return body if isinstance(body, bytes) else bytes(body or b"")

    def delete(self, artifact_id: str) -> bool:
        existed = self.get(artifact_id) is not None
        self.client.delete_object(Bucket=self.bucket, Key=self._key(artifact_id))
        return existed

    def list(self) -> list[ArtifactRecord]:
        response = self.client.list_objects_v2(Bucket=self.bucket, Prefix=self._prefix_for_list())
        records: list[ArtifactRecord] = []
        for item in response.get("Contents", []):
            key = item.get("Key", "")
            if not key or key.endswith("/"):
                continue
            metadata = self._read_metadata(key)
            artifact_id = key.rsplit("/", 1)[-1]
            records.append(ArtifactRecord(artifact_id=artifact_id, path=self._uri(key), metadata=metadata))
        return records

    def _key(self, artifact_id: str) -> str:
        safe_id = safe_artifact_id(artifact_id)
        return f"{self.prefix}/{safe_id}" if self.prefix else safe_id

    def _prefix_for_list(self) -> str:
        return f"{self.prefix}/" if self.prefix else ""

    def _uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def _encode_metadata(self, metadata: dict[str, Any]) -> dict[str, str]:
        if not metadata:
            return {}
        return {"voicebot_metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True)}

    def _read_metadata(self, key: str) -> dict[str, Any]:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # pragma: no cover - concrete SDK exceptions vary by client
            if _is_not_found_error(exc):
                return {}
            raise
        encoded = response.get("Metadata", {}).get("voicebot_metadata", "")
        if not encoded:
            return {}
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError:
            self.load_diagnostics["skipped_malformed_metadata"] += 1
            return {}
        return payload if isinstance(payload, dict) else {}


def safe_artifact_id(value: str) -> str:
    text = value.strip().replace("\\", "_").replace("/", "_")
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in text) or "artifact"


def _s3_client(*, endpoint_url: str = "", region_name: str = "") -> Any:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - depends on optional deployment dependency
        from .errors import StorageUnavailable

        raise StorageUnavailable(
            "boto3 package is not installed",
            family="audio_artifacts",
            driver="s3",
        ) from exc
    kwargs = {}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    if region_name:
        kwargs["region_name"] = region_name
    return boto3.client("s3", **kwargs)


def _is_not_found_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        return str(code) in {"404", "NoSuchKey", "NotFound"}
    return isinstance(exc, (FileNotFoundError, KeyError))
