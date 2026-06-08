from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter

from .api_models import ConversationEvaluationRequest
from .observability import ConversationExpectation, build_timeline, diagnostics_summary, evaluate_conversation, evaluate_slos


@dataclass(frozen=True)
class ObservabilityApiContext:
    events: Any
    transcripts: Any
    durable_call_events: Callable[..., list[Any]]
    validated_limit: Callable[[int], int]


def create_observability_router(context: ObservabilityApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/observability/timeline")
    def observability_timeline(
        after: int = 0,
        call_id: str | None = None,
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
        session_id: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        return observability_timeline_payload(
            context,
            after=after,
            call_id=call_id,
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            session_id=session_id,
            limit=limit,
        )

    @router.post("/observability/evaluate")
    def observability_evaluate(request: ConversationEvaluationRequest) -> dict[str, Any]:
        return observability_evaluate_payload(context, request)

    @router.get("/observability/slo")
    def observability_slo(
        call_id: str | None = None,
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
        session_id: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        return observability_slo_payload(
            context,
            call_id=call_id,
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            session_id=session_id,
            limit=limit,
        )

    @router.get("/observability/diagnostics")
    def observability_diagnostics(
        call_id: str | None = None,
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
        session_id: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        return observability_diagnostics_payload(
            context,
            call_id=call_id,
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            session_id=session_id,
            limit=limit,
        )

    return router


def observability_timeline_payload(
    context: ObservabilityApiContext,
    *,
    after: int = 0,
    call_id: str | None = None,
    workspace_id: str | None = None,
    voicebot_id: str | None = None,
    session_id: str | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    checked_limit = context.validated_limit(limit)
    if call_id:
        return build_timeline(
            context.durable_call_events(context.events, context.transcripts, call_id, after=after, limit=checked_limit)
        )
    return build_timeline(
        context.events.list_events(
            after=after,
            call_id=call_id,
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            session_id=session_id,
            limit=checked_limit,
        )
    )


def observability_evaluate_payload(
    context: ObservabilityApiContext,
    request: ConversationEvaluationRequest,
) -> dict[str, Any]:
    return evaluate_conversation(
        context.events.list_events(
            after=request.after,
            call_id=request.call_id,
            workspace_id=request.workspace_id,
            voicebot_id=request.voicebot_id,
            session_id=request.session_id,
            limit=context.validated_limit(request.limit),
        ),
        ConversationExpectation(
            must_include_event_types=tuple(request.must_include_event_types),
            max_duplicate_agent_responses=request.max_duplicate_agent_responses,
            require_final_agent_response=request.require_final_agent_response,
        ),
    )


def observability_slo_payload(
    context: ObservabilityApiContext,
    *,
    call_id: str | None = None,
    workspace_id: str | None = None,
    voicebot_id: str | None = None,
    session_id: str | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    return evaluate_slos(
        context.events.list_events(
            call_id=call_id,
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            session_id=session_id,
            limit=context.validated_limit(limit),
        )
    )


def observability_diagnostics_payload(
    context: ObservabilityApiContext,
    *,
    call_id: str | None = None,
    workspace_id: str | None = None,
    voicebot_id: str | None = None,
    session_id: str | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    return diagnostics_summary(
        context.events.list_events(
            call_id=call_id,
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
            session_id=session_id,
            limit=context.validated_limit(limit),
        )
    )
