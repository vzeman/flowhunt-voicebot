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
    EventCatalogEntry("stt_result_dropped", "stt", "Speech-to-text result was not sent to the agent because a newer caller turn superseded it."),
    EventCatalogEntry("user_transcript_partial", "stt", "Partial recognized caller text from a streaming STT provider."),
    EventCatalogEntry("user_transcript", "stt", "Final recognized caller text."),
    EventCatalogEntry("agent_response_requested", "agent", "Agent should decide what to do for a call event or user turn."),
    EventCatalogEntry("agent_response_partial", "agent", "Partial text response from a streaming agent."),
    EventCatalogEntry("agent_response_received", "agent", "Service received a final response from an agent."),
    EventCatalogEntry("agent_response_deferred", "agent", "Agent response was held until caller speech stopped."),
    EventCatalogEntry("agent_response_dropped", "agent", "Agent response was intentionally not played."),
    EventCatalogEntry("agent_response_queued", "agent", "Agent response audio was queued for playback."),
    EventCatalogEntry("agent_task_claimed", "agent", "Agent worker claimed an agent response task."),
    EventCatalogEntry("agent_task_renewed", "agent", "Agent worker renewed an active task claim."),
    EventCatalogEntry("agent_task_released", "agent", "Agent worker released a previously claimed task."),
    EventCatalogEntry("flowhunt_issue_created", "agent_team", "Voice agent created a FlowHunt AI Project issue."),
    EventCatalogEntry("flowhunt_issue_updated", "agent_team", "FlowHunt AI Project issue returned an interim update."),
    EventCatalogEntry("flowhunt_issue_completed", "agent_team", "FlowHunt AI Project issue returned a result."),
    EventCatalogEntry("flowhunt_flow_invoked", "agent_team", "Voice agent invoked a FlowHunt Flow for a complex request."),
    EventCatalogEntry("flowhunt_flow_updated", "agent_team", "FlowHunt Flow invocation returned an interim update."),
    EventCatalogEntry("flowhunt_flow_completed", "agent_team", "FlowHunt Flow invocation returned a result."),
    EventCatalogEntry("provider_call_failed", "providers", "Provider adapter call failed with typed error metadata."),
    EventCatalogEntry("runtime_config_updated", "config", "Workspace voicebot runtime config was validated and activated."),
    EventCatalogEntry("session_lease_acquired", "session_ownership", "Runtime worker acquired ownership for an active session."),
    EventCatalogEntry("session_lease_renewed", "session_ownership", "Runtime worker renewed ownership for an active session."),
    EventCatalogEntry("session_lease_released", "session_ownership", "Runtime worker released active session ownership."),
    EventCatalogEntry("session_lease_expired", "session_ownership", "Active session ownership lease expired."),
    EventCatalogEntry("session_lease_lost", "session_ownership", "Runtime detected that a live media session no longer has a valid owner lease."),
    EventCatalogEntry("session_recovered", "session_ownership", "Non-media session work was recovered after owner loss or restart."),
    EventCatalogEntry("session_interrupted", "session_ownership", "Live media was interrupted because active session ownership could not be recovered."),
    EventCatalogEntry("session_admission_decided", "routing", "Runtime accepted, rejected, queued, or fallback-routed an incoming session."),
    EventCatalogEntry("subagent_task_requested", "agent_team", "Delegated subagent task was requested."),
    EventCatalogEntry("subagent_task_deduplicated", "agent_team", "Delegated subagent task reused an existing task."),
    EventCatalogEntry("subagent_task_updated", "agent_team", "Delegated subagent task changed state or progress."),
    EventCatalogEntry("subagent_task_completed", "agent_team", "Delegated subagent task completed successfully."),
    EventCatalogEntry("subagent_task_failed", "agent_team", "Delegated subagent task failed."),
    EventCatalogEntry("subagent_task_timed_out", "agent_team", "Delegated subagent task exceeded its timeout."),
    EventCatalogEntry("subagent_task_cancelled", "agent_team", "Delegated subagent task was cancelled."),
    EventCatalogEntry(
        "subagent_task_late_completed",
        "agent_team",
        "Delegated subagent task completed after the call/session ended.",
    ),
    EventCatalogEntry("tts_started", "tts", "Text-to-speech synthesis started."),
    EventCatalogEntry("tts_finished", "tts", "Text-to-speech synthesis finished."),
    EventCatalogEntry("tts_failed", "tts", "Text-to-speech synthesis failed."),
    EventCatalogEntry("bot_playback_started", "playback", "Bot audio started playing to the call."),
    EventCatalogEntry("bot_playback_interrupted", "playback", "Bot playback was interrupted by caller speech or control."),
    EventCatalogEntry("bot_playback_finished", "playback", "Queued bot audio finished playing."),
    EventCatalogEntry("metrics", "telemetry", "Timing or operational metric emitted by the runtime."),
    EventCatalogEntry("dtmf", "caller_media", "Caller sent a DTMF digit."),
    EventCatalogEntry("multimodal_content_added", "agent", "Normalized mixed-modal content was attached to a call context."),
    EventCatalogEntry("transport_error", "transport", "SIP, AudioSocket, or WebRTC transport emitted an error."),
    EventCatalogEntry("system", "system", "Operational fallback event for unexpected or low-level conditions."),
    EventCatalogEntry("context_compacted", "context", "Long event context was summarized."),
)


def event_catalog() -> list[dict[str, object]]:
    return [entry.to_dict() for entry in EVENT_CATALOG]


def missing_catalog_event_types() -> set[str]:
    catalog_types = {entry.type for entry in EVENT_CATALOG}
    declared_types = set(get_args(EventType))
    return declared_types - catalog_types


def event_catalog_integrity_issues(catalog: tuple[EventCatalogEntry, ...] = EVENT_CATALOG) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    declared_types = set(get_args(EventType))
    seen: set[str] = set()
    for entry in catalog:
        if not entry.type.strip():
            issues.append({**entry.to_dict(), "issue": "event type is required"})
            continue
        if entry.type in seen:
            issues.append({**entry.to_dict(), "issue": "duplicate event catalog type"})
        seen.add(entry.type)
        if entry.type not in declared_types:
            issues.append({**entry.to_dict(), "issue": "event type is not declared by runtime"})
        if not entry.category.strip():
            issues.append({**entry.to_dict(), "issue": "event category is required"})
        if not entry.description.strip():
            issues.append({**entry.to_dict(), "issue": "event description is required"})

    for missing_type in sorted(declared_types - seen):
        issues.append(
            {
                "type": missing_type,
                "category": "",
                "description": "",
                "agent_visible": True,
                "issue": "declared event type missing from catalog",
            }
        )
    return issues
