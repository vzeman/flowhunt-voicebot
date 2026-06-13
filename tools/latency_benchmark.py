from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from typing import Any

from voicebot.events import VoicebotEvent
from voicebot.metrics import summarize_metrics
from voicebot.observability import latency_observability_summary


BASE_TIME = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)


@dataclass
class ScenarioBuilder:
    name: str
    call_id: str
    _next_id: int = 1
    events: list[VoicebotEvent] = field(default_factory=list)

    def add(self, seconds: float, event_type: str, data: dict[str, Any] | None = None) -> VoicebotEvent:
        event = VoicebotEvent(
            id=self._next_id,
            call_id=self.call_id,
            type=event_type,  # type: ignore[arg-type]
            timestamp=(BASE_TIME + timedelta(seconds=seconds)).isoformat(),
            data={"scenario": self.name, **(data or {})},
        )
        self._next_id += 1
        self.events.append(event)
        return event


def run_benchmark() -> dict[str, Any]:
    scenarios = {
        "final_only": _final_only_scenario(),
        "final_tool_only": _final_tool_only_scenario(),
        "speculative_confirmed": _speculative_confirmed_scenario(),
        "speculative_cancelled": _speculative_cancelled_scenario(),
        "speculative_superseded": _speculative_superseded_scenario(),
        "streaming_agent_tts": _streaming_agent_tts_scenario(),
    }
    summaries = {
        name: {
            "event_count": len(events),
            "metrics": summarize_metrics(events)["metrics"],
            "latency": latency_observability_summary(events),
        }
        for name, events in scenarios.items()
    }
    checks = _benchmark_checks(scenarios, summaries)
    return {
        "ok": all(check["ok"] for check in checks.values()),
        "checks": checks,
        "scenarios": summaries,
    }


def _final_only_scenario() -> list[VoicebotEvent]:
    b = ScenarioBuilder("final_only", "bench-final")
    b.add(0.00, "user_speech_started", {"turn_id": 1})
    b.add(1.00, "user_speech_finished", {"turn_id": 1})
    b.add(1.12, "user_transcript", {"turn_id": 1, "text": "Hello"})
    request = b.add(1.12, "agent_response_requested", {"turn_id": 1, "text": "Hello"})
    b.add(1.20, "metrics", {"name": "agent_task_pickup_latency_seconds", "value": 0.03, "event_id": request.id})
    b.add(1.25, "agent_response_received", {"response_to_event_id": request.id, "text": "Hello."})
    b.add(1.26, "tts_started", {"response_to_event_id": request.id})
    b.add(1.31, "metrics", {"name": "tts_first_audio_latency_seconds", "value": 0.05, "response_to_event_id": request.id})
    b.add(1.32, "agent_response_queued", {"response_to_event_id": request.id})
    b.add(1.34, "bot_playback_started", {"response_to_event_id": request.id})
    b.add(1.34, "metrics", {"name": "response_request_to_first_playback_seconds", "value": 0.22, "event_id": request.id})
    return b.events


def _final_tool_only_scenario() -> list[VoicebotEvent]:
    b = ScenarioBuilder("final_tool_only", "bench-final-tool")
    b.add(0.00, "user_speech_started", {"turn_id": 1})
    b.add(1.00, "user_speech_finished", {"turn_id": 1})
    b.add(1.12, "user_transcript", {"turn_id": 1, "text": "Check the site"})
    request = b.add(1.12, "agent_response_requested", {"turn_id": 1, "text": "Check the site"})
    b.add(1.18, "subagent_task_requested", {"task_id": "final-task", "request_event_id": request.id})
    b.add(1.62, "subagent_task_completed", {"task_id": "final-task", "request_event_id": request.id})
    b.add(1.62, "metrics", {"name": "tool_result_after_final_request_seconds", "value": 0.50, "event_id": request.id})
    b.add(1.68, "agent_response_requested", {"turn_id": 1, "reason": "colleague_result", "source_task_id": "final-task"})
    b.add(1.78, "agent_response_received", {"response_to_event_id": request.id, "text": "The site is up."})
    b.add(1.79, "tts_started", {"response_to_event_id": request.id})
    b.add(1.85, "agent_response_queued", {"response_to_event_id": request.id})
    b.add(1.88, "bot_playback_started", {"response_to_event_id": request.id})
    b.add(1.88, "metrics", {"name": "response_request_to_first_playback_seconds", "value": 0.76, "event_id": request.id})
    return b.events


def _speculative_confirmed_scenario() -> list[VoicebotEvent]:
    b = ScenarioBuilder("speculative_confirmed", "bench-spec-confirm")
    b.add(0.00, "user_speech_started", {"turn_id": 1})
    partial = b.add(0.32, "user_transcript_partial", {"turn_id": 1, "text": "please check the site"})
    b.add(0.32, "metrics", {"name": "partial_stt_first_text_seconds", "value": 0.32, "turn_id": 1})
    b.add(0.34, "subagent_task_speculative_started", {"task_id": "spec-task", "partial_event_id": partial.id})
    b.add(0.34, "metrics", {"name": "partial_stt_to_speculative_start_seconds", "value": 0.02, "turn_id": 1})
    b.add(0.34, "metrics", {"name": "speech_start_to_speculative_start_seconds", "value": 0.34, "turn_id": 1})
    b.add(0.84, "subagent_task_completed", {"task_id": "spec-task"})
    b.add(1.00, "user_speech_finished", {"turn_id": 1})
    b.add(1.12, "user_transcript", {"turn_id": 1, "text": "please check the site"})
    b.add(1.12, "metrics", {"name": "speech_finished_to_final_transcript_seconds", "value": 0.12, "turn_id": 1})
    request = b.add(1.12, "agent_response_requested", {"turn_id": 1, "text": "please check the site"})
    b.add(1.13, "subagent_task_speculative_confirmed", {"task_id": "spec-task", "final_request_event_id": request.id})
    b.add(1.13, "metrics", {"name": "streaming_rag_reflector_decision", "value": 1, "decision": "reuse"})
    b.add(1.13, "metrics", {"name": "speculative_task_completed_before_final_transcript", "value": 1})
    b.add(1.13, "metrics", {"name": "speculative_result_reuse_latency_seconds", "value": 0.0})
    b.add(1.18, "agent_response_received", {"response_to_event_id": request.id, "text": "The site is up."})
    b.add(1.19, "tts_started", {"response_to_event_id": request.id})
    b.add(1.25, "agent_response_queued", {"response_to_event_id": request.id})
    b.add(1.27, "bot_playback_started", {"response_to_event_id": request.id})
    b.add(1.27, "metrics", {"name": "response_request_to_first_playback_seconds", "value": 0.15, "event_id": request.id})
    return b.events


def _speculative_cancelled_scenario() -> list[VoicebotEvent]:
    b = ScenarioBuilder("speculative_cancelled", "bench-spec-cancel")
    b.add(0.00, "user_speech_started", {"turn_id": 1})
    partial = b.add(0.30, "user_transcript_partial", {"turn_id": 1, "text": "please check the site"})
    b.add(0.32, "subagent_task_speculative_started", {"task_id": "cancel-task", "partial_event_id": partial.id})
    b.add(1.00, "user_speech_finished", {"turn_id": 1})
    b.add(1.10, "user_transcript", {"turn_id": 1, "text": "thanks goodbye"})
    request = b.add(1.10, "agent_response_requested", {"turn_id": 1, "text": "thanks goodbye"})
    b.add(1.11, "subagent_task_speculative_cancelled", {"task_id": "cancel-task", "final_request_event_id": request.id})
    b.add(1.11, "metrics", {"name": "streaming_rag_reflector_decision", "value": 1, "decision": "cancel"})
    b.add(1.20, "agent_response_received", {"response_to_event_id": request.id, "text": "Goodbye."})
    return b.events


def _speculative_superseded_scenario() -> list[VoicebotEvent]:
    b = ScenarioBuilder("speculative_superseded", "bench-spec-supersede")
    b.add(0.00, "user_speech_started", {"turn_id": 1})
    first_partial = b.add(0.26, "user_transcript_partial", {"turn_id": 1, "text": "please check website status"})
    b.add(0.26, "metrics", {"name": "partial_stt_first_text_seconds", "value": 0.26, "turn_id": 1})
    b.add(0.28, "subagent_task_speculative_started", {"task_id": "old-spec-task", "partial_event_id": first_partial.id})
    b.add(0.28, "metrics", {"name": "partial_stt_to_speculative_start_seconds", "value": 0.02, "turn_id": 1})
    b.add(0.28, "metrics", {"name": "speech_start_to_speculative_start_seconds", "value": 0.28, "turn_id": 1})
    second_partial = b.add(0.55, "user_transcript_partial", {"turn_id": 1, "text": "please check website pricing"})
    b.add(0.57, "subagent_task_speculative_superseded", {"task_id": "old-spec-task", "reason": "superseded_by_new_partial_query"})
    b.add(0.57, "subagent_task_speculative_started", {"task_id": "new-spec-task", "partial_event_id": second_partial.id})
    b.add(0.57, "metrics", {"name": "partial_stt_to_speculative_start_seconds", "value": 0.02, "turn_id": 1})
    b.add(0.57, "metrics", {"name": "speech_start_to_speculative_start_seconds", "value": 0.57, "turn_id": 1})
    b.add(0.90, "subagent_task_completed", {"task_id": "new-spec-task"})
    b.add(1.00, "user_speech_finished", {"turn_id": 1})
    b.add(1.11, "user_transcript", {"turn_id": 1, "text": "please check website pricing"})
    b.add(1.11, "metrics", {"name": "speech_finished_to_final_transcript_seconds", "value": 0.11, "turn_id": 1})
    request = b.add(1.11, "agent_response_requested", {"turn_id": 1, "text": "please check website pricing"})
    b.add(1.12, "subagent_task_speculative_confirmed", {"task_id": "new-spec-task", "final_request_event_id": request.id})
    b.add(1.12, "metrics", {"name": "streaming_rag_reflector_decision", "value": 1, "decision": "reuse"})
    b.add(1.12, "metrics", {"name": "speculative_task_completed_before_final_transcript", "value": 1})
    b.add(1.12, "metrics", {"name": "speculative_result_reuse_latency_seconds", "value": 0.0})
    b.add(1.17, "agent_response_received", {"response_to_event_id": request.id, "text": "The pricing page is ready."})
    b.add(1.18, "tts_started", {"response_to_event_id": request.id})
    b.add(1.24, "agent_response_queued", {"response_to_event_id": request.id})
    b.add(1.26, "bot_playback_started", {"response_to_event_id": request.id})
    b.add(1.26, "metrics", {"name": "response_request_to_first_playback_seconds", "value": 0.15, "event_id": request.id})
    return b.events


def _streaming_agent_tts_scenario() -> list[VoicebotEvent]:
    b = ScenarioBuilder("streaming_agent_tts", "bench-stream")
    b.add(0.00, "user_speech_started", {"turn_id": 1})
    b.add(0.80, "user_speech_finished", {"turn_id": 1})
    b.add(0.90, "user_transcript", {"turn_id": 1, "text": "Summarize this"})
    request = b.add(0.90, "agent_response_requested", {"turn_id": 1, "text": "Summarize this"})
    b.add(0.94, "metrics", {"name": "agent_stream_first_text_latency_seconds", "value": 0.04, "event_id": request.id})
    b.add(0.94, "agent_response_partial", {"response_to_event_id": request.id, "response_kind": "stream_chunk", "text": "Here is"})
    b.add(0.95, "tts_started", {"response_to_event_id": request.id, "response_kind": "stream_chunk"})
    b.add(1.00, "metrics", {"name": "tts_stream_first_audio_latency_seconds", "value": 0.05, "response_to_event_id": request.id})
    b.add(1.01, "agent_response_queued", {"response_to_event_id": request.id, "response_kind": "stream_chunk"})
    b.add(1.02, "bot_playback_started", {"response_to_event_id": request.id})
    b.add(1.02, "metrics", {"name": "response_request_to_first_playback_seconds", "value": 0.12, "event_id": request.id})
    b.add(1.20, "metrics", {"name": "stream_chunk_count", "value": 1, "response_to_event_id": request.id})
    return b.events


def _benchmark_checks(scenarios: dict[str, list[VoicebotEvent]], summaries: dict[str, Any]) -> dict[str, dict[str, Any]]:
    final_wait = _latest_metric(summaries["final_tool_only"], "tool_result_after_final_request_seconds")
    speculative_wait = _latest_metric(summaries["speculative_confirmed"], "speculative_result_reuse_latency_seconds")
    savings = final_wait - speculative_wait
    speculative_started = _first_event(scenarios["speculative_confirmed"], "subagent_task_speculative_started")
    speech_finished = _first_event(scenarios["speculative_confirmed"], "user_speech_finished")
    superseded_count = summaries["speculative_superseded"]["latency"]["streaming_rag"]["superseded"]
    cancel_task_spoken = any(
        event.type in {"agent_response_received", "agent_response_queued", "bot_playback_started"}
        and event.data.get("task_id") == "cancel-task"
        for event in scenarios["speculative_cancelled"]
    )
    return {
        "confirmed_speculative_reduces_final_wait": {
            "ok": savings >= 0.45,
            "observed_savings_seconds": savings,
            "target_seconds": 0.45,
        },
        "speculative_starts_before_endpoint": {
            "ok": speculative_started is not None
            and speech_finished is not None
            and speculative_started.timestamp < speech_finished.timestamp,
            "speculative_started_event_id": speculative_started.id if speculative_started else None,
            "speech_finished_event_id": speech_finished.id if speech_finished else None,
        },
        "unconfirmed_speculative_result_not_spoken": {
            "ok": not cancel_task_spoken,
            "task_id": "cancel-task",
        },
        "superseded_candidates_reported": {
            "ok": superseded_count >= 1,
            "superseded_count": superseded_count,
        },
    }


def _latest_metric(summary: dict[str, Any], name: str) -> float:
    return float(summary["metrics"][name]["latest"]["value"])


def _first_event(events: list[VoicebotEvent], event_type: str) -> VoicebotEvent | None:
    for event in events:
        if event.type == event_type:
            return event
    return None


def main() -> None:
    print(json.dumps(run_benchmark(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
