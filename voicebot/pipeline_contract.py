from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PIPELINE_CONTRACT_VERSION = "2026-05-30.1"

REQUIRED_PIPELINE_STAGES = (
    "transport_input",
    "audio_normalization",
    "turn_detection",
    "stt",
    "communication_agent",
    "subagent_delegation",
    "tts",
    "playback_output",
    "post_output_audit",
)


@dataclass(frozen=True)
class PipelineStageContract:
    name: str
    category: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    local_processors: tuple[str, ...]
    queue_boundary: str
    cancellation_inputs: tuple[str, ...] = ()
    backpressure_key: str = ""
    provider_config_family: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "category": self.category,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "local_processors": list(self.local_processors),
            "queue_boundary": self.queue_boundary,
            "cancellation_inputs": list(self.cancellation_inputs),
            "backpressure_key": self.backpressure_key,
            "notes": self.notes,
        }
        if self.provider_config_family is not None:
            payload["provider_config_family"] = self.provider_config_family
        return payload


PIPELINE_STAGES: tuple[PipelineStageContract, ...] = (
    PipelineStageContract(
        name="transport_input",
        category="transport",
        inputs=("transport_packet", "call_lifecycle", "dtmf"),
        outputs=("audio_input", "call_started", "call_connected", "call_ended", "dtmf"),
        local_processors=("asterisk_audiosocket", "webrtc"),
        queue_boundary="transport_owned",
        cancellation_inputs=("call_ended",),
        backpressure_key="transport_sessions",
        notes="Transport adapters normalize SIP/Asterisk and WebRTC session metadata before media enters the runtime.",
    ),
    PipelineStageContract(
        name="audio_normalization",
        category="audio",
        inputs=("audio_input",),
        outputs=("audio_input", "metrics", "error"),
        local_processors=("resampler", "jitter_buffer", "echo_gate"),
        queue_boundary="in_process",
        cancellation_inputs=("interrupt", "pause_input", "call_ended"),
        backpressure_key="audio_frames",
        notes="Keeps transport-specific packet timing, jitter, and sample-rate handling outside STT and agent code.",
    ),
    PipelineStageContract(
        name="turn_detection",
        category="speech_lifecycle",
        inputs=("audio_input", "interrupt", "resume_input", "pause_input"),
        outputs=("speech_started", "speech_finished", "interrupt", "metrics"),
        local_processors=("vad", "turn_detector"),
        queue_boundary="in_process",
        cancellation_inputs=("interrupt", "call_ended"),
        backpressure_key="active_turns",
        notes="Caller speech during playback emits interruption control frames for the same session.",
    ),
    PipelineStageContract(
        name="stt",
        category="transcription",
        inputs=("speech_finished", "audio_input"),
        outputs=("transcription_started", "transcription_partial", "transcription_finished", "transcription_empty", "user_transcript", "error"),
        local_processors=("stt", "event-log"),
        queue_boundary="queue_ready",
        cancellation_inputs=("cancel_agent", "call_ended"),
        backpressure_key="stt_jobs",
        provider_config_family="stt",
        notes="Provider adapters are selected from workspace and voicebot scoped provider config.",
    ),
    PipelineStageContract(
        name="communication_agent",
        category="agent",
        inputs=("user_transcript", "agent_request", "call_connected", "subagent_task_completed", "call_control_completed"),
        outputs=("agent_response_partial", "agent_response", "agent_response_dropped", "call_control_requested", "metrics", "error"),
        local_processors=("agent-request", "communication-agent"),
        queue_boundary="queue_ready",
        cancellation_inputs=("cancel_agent", "interrupt", "call_ended"),
        backpressure_key="agent_jobs",
        provider_config_family="agent",
        notes="The communication agent owns customer-facing wording and can request subagent/tool work through provider-neutral tasks.",
    ),
    PipelineStageContract(
        name="subagent_delegation",
        category="external_task",
        inputs=("call_control_requested", "subagent_task_requested", "agent_response"),
        outputs=("subagent_task_started", "subagent_task_updated", "subagent_task_completed", "subagent_task_failed", "agent_request", "error"),
        local_processors=("subagent-coordinator", "task-lifecycle"),
        queue_boundary="durable_queue_required",
        cancellation_inputs=("call_ended",),
        backpressure_key="subagent_tasks",
        provider_config_family="subagent",
        notes="FlowHunt is one provider. The contract keeps future colleague/task providers behind the same lifecycle.",
    ),
    PipelineStageContract(
        name="tts",
        category="tts",
        inputs=("agent_response", "agent_response_partial"),
        outputs=("tts_started", "audio_output", "tts_finished", "tts_failed", "metrics"),
        local_processors=("tts", "tts-cache"),
        queue_boundary="queue_ready",
        cancellation_inputs=("cancel_tts", "interrupt", "call_ended"),
        backpressure_key="tts_jobs",
        provider_config_family="tts",
        notes="Supports chunked and streaming output; generated audio may be cached by normalized text and voice config.",
    ),
    PipelineStageContract(
        name="playback_output",
        category="playback",
        inputs=("audio_output", "flush_playback", "interrupt"),
        outputs=("playback_started", "playback_interrupted", "playback_finished", "call_control_completed", "metrics", "error"),
        local_processors=("playback", "transport-output"),
        queue_boundary="transport_owned",
        cancellation_inputs=("flush_playback", "interrupt", "call_ended"),
        backpressure_key="playback_buffers",
        notes="Playback is transport-specific only at the final media sink and must remain interruptible per session.",
    ),
    PipelineStageContract(
        name="post_output_audit",
        category="audit",
        inputs=("call_ended", "metrics", "error", "playback_finished", "call_control_completed"),
        outputs=("transcript_row", "observability_timeline", "retention_artifact"),
        local_processors=("event-log", "transcript-store", "metrics"),
        queue_boundary="queue_ready",
        cancellation_inputs=(),
        backpressure_key="audit_jobs",
        notes="Post-output work is detached from active media once the customer-facing output is complete.",
    ),
)


TRANSPORT_PIPELINE_MAPPING: dict[str, tuple[str, ...]] = {
    "asterisk_audiosocket": tuple(stage.name for stage in PIPELINE_STAGES),
    "webrtc": tuple(stage.name for stage in PIPELINE_STAGES),
}


def pipeline_contract_payload() -> dict[str, Any]:
    return {
        "version": PIPELINE_CONTRACT_VERSION,
        "stages": [stage.to_dict() for stage in PIPELINE_STAGES],
        "transports": {
            name: {"stages": list(stages), "same_conceptual_pipeline": True}
            for name, stages in sorted(TRANSPORT_PIPELINE_MAPPING.items())
        },
        "local_development": {
            "mode": "single_process_with_explicit_queue_boundaries",
            "supported": True,
        },
        "production": {
            "ready_for_external_workers": False,
            "required_before_kubernetes": [
                "durable queue provider for queue_ready and durable_queue_required boundaries",
                "lease-backed session ownership checks around transport-owned stages",
                "provider workers that preserve frame correlation and cancellation metadata",
                "transport routing that can map SIP and WebRTC sessions to owning workers",
            ],
        },
    }


def pipeline_contract_issues() -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not PIPELINE_CONTRACT_VERSION:
        issues.append({"issue": "pipeline contract version is empty"})

    stage_names = [stage.name for stage in PIPELINE_STAGES]
    duplicates = sorted({name for name in stage_names if stage_names.count(name) > 1})
    for duplicate in duplicates:
        issues.append({"stage": duplicate, "issue": "duplicate stage name"})

    for required in REQUIRED_PIPELINE_STAGES:
        if required not in stage_names:
            issues.append({"stage": required, "issue": "required stage is missing"})

    for stage in PIPELINE_STAGES:
        if not stage.inputs:
            issues.append({"stage": stage.name, "issue": "inputs are empty"})
        if not stage.outputs:
            issues.append({"stage": stage.name, "issue": "outputs are empty"})
        if not stage.local_processors:
            issues.append({"stage": stage.name, "issue": "local_processors are empty"})
        if not stage.queue_boundary:
            issues.append({"stage": stage.name, "issue": "queue_boundary is empty"})
        if not stage.backpressure_key:
            issues.append({"stage": stage.name, "issue": "backpressure_key is empty"})

    known_stages = set(stage_names)
    required_sequence = tuple(REQUIRED_PIPELINE_STAGES)
    for transport, stages in TRANSPORT_PIPELINE_MAPPING.items():
        unknown = [stage for stage in stages if stage not in known_stages]
        if unknown:
            issues.append({"transport": transport, "issue": "unknown stages", "stages": unknown})
        if tuple(stages) != required_sequence:
            issues.append({"transport": transport, "issue": "transport does not map to canonical stage sequence"})
    return issues
