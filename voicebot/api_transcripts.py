from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter


@dataclass(frozen=True)
class TranscriptsApiContext:
    transcripts: Any
    optional_int_value: Callable[[Any, str, int], int]
    validated_limit: Callable[[int], int]


def create_transcripts_router(context: TranscriptsApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/calls/{call_id}/transcript")
    def call_transcript(call_id: str, after: Any = 0, limit: Any = 200) -> dict[str, Any]:
        return call_transcript_payload(context, call_id, after=after, limit=limit)

    @router.get("/transcripts")
    def list_transcripts() -> dict[str, Any]:
        return list_transcripts_payload(context)

    @router.get("/transcripts/summary")
    def transcript_summaries(after_call_id: str | None = None, limit: Any = 200) -> dict[str, Any]:
        return transcript_summaries_payload(context, after_call_id=after_call_id, limit=limit)

    @router.get("/transcripts/stats")
    def transcript_stats(after_call_id: str | None = None, limit: Any = 200) -> dict[str, Any]:
        return transcript_stats_payload(context, after_call_id=after_call_id, limit=limit)

    return router


def call_transcript_payload(
    context: TranscriptsApiContext,
    call_id: str,
    *,
    after: Any = 0,
    limit: Any = 200,
) -> dict[str, Any]:
    return {
        "call_id": call_id,
        "events": context.transcripts.read(
            call_id,
            after=context.optional_int_value(after, "after", 0),
            limit=context.validated_limit(context.optional_int_value(limit, "limit", 200)),
        ),
    }


def list_transcripts_payload(context: TranscriptsApiContext) -> dict[str, Any]:
    return {"call_ids": context.transcripts.list_call_ids()}


def transcript_summaries_payload(
    context: TranscriptsApiContext,
    *,
    after_call_id: str | None = None,
    limit: Any = 200,
) -> dict[str, Any]:
    return {
        "transcripts": context.transcripts.summaries(
            after_call_id=after_call_id,
            limit=context.validated_limit(context.optional_int_value(limit, "limit", 200)),
        )
    }


def transcript_stats_payload(
    context: TranscriptsApiContext,
    *,
    after_call_id: str | None = None,
    limit: Any = 200,
) -> dict[str, Any]:
    return context.transcripts.stats(
        after_call_id=after_call_id,
        limit=context.validated_limit(context.optional_int_value(limit, "limit", 200)),
    )
