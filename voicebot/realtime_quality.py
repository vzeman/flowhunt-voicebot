from __future__ import annotations

from typing import Any

from .config import Settings


def realtime_audio_profile(settings: Settings) -> dict[str, Any]:
    return {
        "turn_detection": {
            "start_threshold": settings.start_threshold,
            "stop_threshold": settings.stop_threshold,
            "vad_start_ms": settings.vad_start_ms,
            "silence_ms": settings.silence_ms,
            "min_seconds": settings.min_seconds,
            "max_seconds": settings.max_seconds,
            "barge_in_threshold": settings.barge_in_threshold,
            "echo_tail_ms": settings.echo_tail_ms,
        },
        "cancellation": {
            "barge_in_interrupts_playback": True,
            "stale_stt_results_dropped": True,
            "stale_agent_responses_dropped": True,
            "tts_chunks_cancel_after_new_user_activity": True,
            "worker_queue_cancellation_ready": True,
        },
        "streaming": {
            "stt_partial_transcripts_supported_by_contract": True,
            "tts_streaming_chunks_supported": True,
            "tts_chunk_chars": settings.tts_chunk_chars,
            "first_audio_metric": "tts_first_audio_latency_seconds",
            "end_to_end_first_audio_metric": "end_of_speech_to_playback_started_seconds",
        },
        "normalization": {
            "webrtc_jitter_buffer_enabled": settings.webrtc_jitter_buffer_enabled,
            "webrtc_jitter_target_delay_ms": settings.webrtc_jitter_target_delay_ms,
            "webrtc_jitter_max_delay_ms": settings.webrtc_jitter_max_delay_ms,
            "audiosocket_jitter_buffer_enabled": settings.audiosocket_jitter_buffer_enabled,
            "audiosocket_jitter_target_delay_ms": settings.audiosocket_jitter_target_delay_ms,
            "audiosocket_jitter_max_delay_ms": settings.audiosocket_jitter_max_delay_ms,
        },
        "cache": {
            "tts_cache_enabled": settings.tts_cache_enabled,
            "tts_cache_dir": settings.tts_cache_dir,
            "local_restart_survives": settings.tts_cache_enabled,
            "production_backend_target": "object_storage_or_cdn_cache",
        },
        "regression_coverage": [
            "vad",
            "barge_in",
            "echo_tail",
            "no_text",
            "jitter_buffer",
            "streaming_tts_stale_chunk_drop",
            "tts_cache",
            "first_audio_latency_metrics",
        ],
    }


def realtime_audio_profile_issues(profile: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    turn = profile.get("turn_detection") or {}
    if turn.get("stop_threshold", 0) > turn.get("start_threshold", 0):
        issues.append({"issue": "stop_threshold exceeds start_threshold"})
    if turn.get("barge_in_threshold", 0) < 0:
        issues.append({"issue": "barge_in_threshold is negative"})
    cancellation = profile.get("cancellation") or {}
    for key in ("barge_in_interrupts_playback", "stale_stt_results_dropped", "stale_agent_responses_dropped", "tts_chunks_cancel_after_new_user_activity"):
        if not cancellation.get(key):
            issues.append({"issue": "cancellation capability is disabled", "capability": key})
    streaming = profile.get("streaming") or {}
    if streaming.get("tts_chunk_chars", 0) <= 0:
        issues.append({"issue": "tts_chunk_chars must be positive"})
    normalization = profile.get("normalization") or {}
    for prefix in ("webrtc", "audiosocket"):
        target = normalization.get(f"{prefix}_jitter_target_delay_ms", 0)
        maximum = normalization.get(f"{prefix}_jitter_max_delay_ms", 0)
        if maximum < target:
            issues.append({"issue": "jitter max delay is lower than target delay", "transport": prefix})
    if not profile.get("cache", {}).get("tts_cache_enabled"):
        issues.append({"issue": "tts cache is disabled"})
    return issues
