from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

from .events import EventType


@dataclass(frozen=True)
class EventCatalogEntry:
    type: str
    category: str
    description: str
    agent_visible: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "type": self.type,
            "category": self.category,
            "description": self.description,
            "agent_visible": self.agent_visible,
        }


EVENT_CATALOG: tuple[EventCatalogEntry, ...] = (
    EventCatalogEntry("call_started", "call_lifecycle", "AudioSocket session was created and call ID is known."),
    EventCatalogEntry("call_connected", "call_lifecycle", "Call media is connected and the agent may greet the caller."),
    EventCatalogEntry("call_ended", "call_lifecycle", "AudioSocket session ended."),
    EventCatalogEntry("call_control_requested", "call_control", "Agent or API requested a call control action."),
    EventCatalogEntry("call_control_completed", "call_control", "Asterisk returned a result for a call control action."),
    EventCatalogEntry("user_speech_started", "caller_media", "VAD detected caller speech."),
    EventCatalogEntry("user_speech_finished", "caller_media", "VAD detected the end of caller speech."),
    EventCatalogEntry("stt_started", "stt", "Speech-to-text started for a speech turn."),
    EventCatalogEntry("stt_finished", "stt", "Speech-to-text finished and recognized usable text."),
    EventCatalogEntry("stt_no_text", "stt", "Speech-to-text finished without usable text."),
    EventCatalogEntry("user_transcript_partial", "stt", "Partial recognized caller text from a streaming STT provider."),
    EventCatalogEntry("user_transcript", "stt", "Final recognized caller text."),
    EventCatalogEntry("agent_response_requested", "agent", "Agent should decide what to do for a call event or user turn."),
    EventCatalogEntry("agent_response_partial", "agent", "Partial text response from a streaming agent."),
    EventCatalogEntry("agent_response_received", "agent", "Service received a final response from an agent."),
    EventCatalogEntry("agent_response_dropped", "agent", "Agent response was intentionally not played."),
    EventCatalogEntry("agent_response_queued", "agent", "Agent response audio was queued for playback."),
    EventCatalogEntry("agent_task_claimed", "agent", "Agent worker claimed an agent response task."),
    EventCatalogEntry("agent_task_renewed", "agent", "Agent worker renewed an active task claim."),
    EventCatalogEntry("agent_task_released", "agent", "Agent worker released a previously claimed task."),
    EventCatalogEntry("tts_started", "tts", "Text-to-speech synthesis started."),
    EventCatalogEntry("tts_finished", "tts", "Text-to-speech synthesis finished."),
    EventCatalogEntry("tts_failed", "tts", "Text-to-speech synthesis failed."),
    EventCatalogEntry("bot_playback_started", "playback", "Bot audio started playing to the call."),
    EventCatalogEntry("bot_playback_interrupted", "playback", "Bot playback was interrupted by caller speech or control."),
    EventCatalogEntry("bot_playback_finished", "playback", "Queued bot audio finished playing."),
    EventCatalogEntry("metrics", "telemetry", "Timing or operational metric emitted by the runtime."),
    EventCatalogEntry("dtmf", "caller_media", "Caller sent a DTMF digit."),
    EventCatalogEntry("system", "system", "Operational fallback event for unexpected or low-level conditions."),
    EventCatalogEntry("context_compacted", "context", "Long event context was summarized."),
)


def event_catalog() -> list[dict[str, object]]:
    return [entry.to_dict() for entry in EVENT_CATALOG]


def missing_catalog_event_types() -> set[str]:
    catalog_types = {entry.type for entry in EVENT_CATALOG}
    declared_types = set(get_args(EventType))
    return declared_types - catalog_types
