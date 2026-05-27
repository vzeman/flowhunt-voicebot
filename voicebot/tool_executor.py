from __future__ import annotations

from collections.abc import Awaitable, Callable
import inspect
from typing import Any


ToolHandler = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]


class AgentToolExecutor:
    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, name: str, handler: ToolHandler) -> None:
        self._handlers[name] = handler

    def registered_names(self) -> list[str]:
        return sorted(self._handlers)

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(name)
        result = handler(arguments)
        if inspect.isawaitable(result):
            result = await result
        return result
