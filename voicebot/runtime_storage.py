from __future__ import annotations

from .config import Settings
from .events import EventStore, JsonEventStore
from .transcripts import TranscriptStore


def build_event_store(settings: Settings, transcripts: TranscriptStore) -> EventStore:
    if settings.event_store_provider in {"json", "jsonl"}:
        return JsonEventStore(settings.event_store_path, settings.max_context_events, transcript_store=transcripts)
    if settings.event_store_provider in {"memory", "inmemory", "in-memory"}:
        return EventStore(settings.max_context_events, transcript_store=transcripts)
    raise ValueError(f"Unsupported VOICEBOT_EVENT_STORE_PROVIDER: {settings.event_store_provider}")
