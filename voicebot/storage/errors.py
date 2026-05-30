from __future__ import annotations

from typing import Any, Literal


StorageErrorCode = Literal[
    "unavailable",
    "conflict",
    "not_found",
    "validation_error",
    "timeout",
    "corruption_warning",
]


class StorageError(RuntimeError):
    def __init__(
        self,
        code: StorageErrorCode,
        message: str,
        *,
        family: str | None = None,
        driver: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.family = family
        self.driver = driver
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "family": self.family,
            "driver": self.driver,
            "details": self.details,
        }


class StorageUnavailable(StorageError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__("unavailable", message, **kwargs)


class StorageConflict(StorageError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__("conflict", message, **kwargs)


class StorageNotFound(StorageError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__("not_found", message, **kwargs)


class StorageValidationError(StorageError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__("validation_error", message, **kwargs)


class StorageTimeout(StorageError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__("timeout", message, **kwargs)


class StorageCorruptionWarning(StorageError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__("corruption_warning", message, **kwargs)
