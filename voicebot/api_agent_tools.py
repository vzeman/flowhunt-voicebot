from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException

from .api_models import AgentToolRequest
from .tools import tool_definitions_json_schema, tool_definitions_legacy


@dataclass(frozen=True)
class AgentToolsApiContext:
    tool_executor: Any


def create_agent_tools_router(context: AgentToolsApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/agent/tools")
    def agent_tools() -> dict[str, Any]:
        return agent_tools_payload()

    @router.get("/agent/tools/schema")
    def agent_tool_schema() -> dict[str, Any]:
        return agent_tool_schema_payload()

    @router.post("/agent/tools/{tool_name}")
    async def agent_tool(tool_name: str, request: AgentToolRequest) -> dict[str, Any]:
        return await execute_agent_tool_payload(context, tool_name, request)

    return router


def agent_tools_payload() -> dict[str, Any]:
    return {"tools": tool_definitions_legacy()}


def agent_tool_schema_payload() -> dict[str, Any]:
    return {"tools": tool_definitions_json_schema()}


async def execute_agent_tool_payload(
    context: AgentToolsApiContext,
    tool_name: str,
    request: AgentToolRequest,
) -> dict[str, Any]:
    try:
        return await context.tool_executor.execute(tool_name, request.arguments)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown agent tool: {tool_name}") from None
