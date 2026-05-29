from __future__ import annotations

from .agent_tasks import AgentTaskTracker, JsonAgentTaskTracker
from .config import Settings
from .call_state import CallStateStore, JsonCallStateStore
from .events import EventStore, JsonEventStore
from .scaling import JsonWorkerQueueStore, WorkerQueueStore
from .transcripts import TranscriptStore
from .workspace_model import JsonVoicebotSessionStore, VoicebotSessionStore


def build_event_store(settings: Settings, transcripts: TranscriptStore) -> EventStore:
    if settings.event_store_provider in {"json", "jsonl"}:
        return JsonEventStore(settings.event_store_path, settings.max_context_events, transcript_store=transcripts)
    if settings.event_store_provider in {"memory", "inmemory", "in-memory"}:
        return EventStore(settings.max_context_events, transcript_store=transcripts)
    raise ValueError(f"Unsupported VOICEBOT_EVENT_STORE_PROVIDER: {settings.event_store_provider}")


def build_voicebot_session_store(settings: Settings) -> VoicebotSessionStore:
    if settings.voicebot_session_store_provider in {"json", "jsonl"}:
        return JsonVoicebotSessionStore(settings.voicebot_session_store_path)
    if settings.voicebot_session_store_provider in {"memory", "inmemory", "in-memory"}:
        return VoicebotSessionStore()
    raise ValueError(f"Unsupported VOICEBOT_SESSION_STORE_PROVIDER: {settings.voicebot_session_store_provider}")


def build_agent_task_tracker(settings: Settings) -> AgentTaskTracker:
    if settings.agent_task_store_provider in {"json", "jsonl"}:
        return JsonAgentTaskTracker(
            settings.agent_task_store_path,
            max_responded_event_ids=settings.agent_task_responded_event_retention,
        )
    if settings.agent_task_store_provider in {"memory", "inmemory", "in-memory"}:
        return AgentTaskTracker(settings.agent_task_responded_event_retention)
    raise ValueError(f"Unsupported VOICEBOT_AGENT_TASK_STORE_PROVIDER: {settings.agent_task_store_provider}")


def build_call_state_store(settings: Settings) -> CallStateStore:
    if settings.call_state_store_provider in {"json", "jsonl"}:
        return JsonCallStateStore(settings.call_state_store_path)
    if settings.call_state_store_provider in {"memory", "inmemory", "in-memory"}:
        return CallStateStore()
    raise ValueError(f"Unsupported VOICEBOT_CALL_STATE_STORE_PROVIDER: {settings.call_state_store_provider}")


def build_worker_queue_store(settings: Settings) -> WorkerQueueStore:
    if settings.worker_queue_store_provider in {"json", "jsonl"}:
        return JsonWorkerQueueStore(settings.worker_queue_store_path)
    if settings.worker_queue_store_provider in {"memory", "inmemory", "in-memory"}:
        return WorkerQueueStore()
    raise ValueError(f"Unsupported VOICEBOT_WORKER_QUEUE_STORE_PROVIDER: {settings.worker_queue_store_provider}")
