from __future__ import annotations

from dataclasses import dataclass
from dataclasses import fields
import json
import os
from typing import Any


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_json_list(name: str, default: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return tuple(default)
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError(f"{name} must be a JSON list")
    if not all(isinstance(item, dict) for item in parsed):
        raise ValueError(f"{name} must contain JSON objects")
    return tuple(parsed)


def env_json_list_any(names: tuple[str, ...], default: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return env_json_list(name, default)
    return tuple(default)


def env_csv_tuple(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    api_host: str = os.getenv("VOICEBOT_API_HOST", "0.0.0.0")
    api_port: int = env_int("VOICEBOT_API_PORT", 8080)
    audiosocket_host: str = os.getenv("VOICEBOT_AUDIOSOCKET_HOST", "0.0.0.0")
    audiosocket_port: int = env_int("VOICEBOT_AUDIOSOCKET_PORT", 9019)
    enabled_transports: tuple[str, ...] = env_csv_tuple("VOICEBOT_ENABLED_TRANSPORTS", ("asterisk_audiosocket", "webrtc"))
    webrtc_stun_urls: tuple[str, ...] = env_csv_tuple("VOICEBOT_WEBRTC_STUN_URLS", ("stun:stun.l.google.com:19302",))

    stt_provider: str = os.getenv("VOICEBOT_STT_PROVIDER", "whisper")
    stt_api_key: str = os.getenv("VOICEBOT_STT_API_KEY", "")
    stt_base_url: str = os.getenv("VOICEBOT_STT_BASE_URL", "")
    stt_model: str = os.getenv("VOICEBOT_STT_MODEL", "")
    whisper_model: str = os.getenv("VOICEBOT_WHISPER_MODEL", "base")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("VOICEBOT_OPENAI_BASE_URL", os.getenv("OPENAI_BASE_URL", ""))
    openai_stt_model: str = os.getenv("VOICEBOT_OPENAI_STT_MODEL", "whisper-1")
    language: str | None = os.getenv("VOICEBOT_LANGUAGE", "auto") or None
    stt_prompt: str = os.getenv("VOICEBOT_STT_PROMPT", "")
    chat_mode: str = os.getenv("VOICEBOT_CHAT_MODE", "disabled")
    chat_system_prompt: str = os.getenv("VOICEBOT_CHAT_SYSTEM_PROMPT", "")
    chat_response_prompt: str = os.getenv(
        "VOICEBOT_CHAT_RESPONSE_PROMPT",
        "When chat is enabled, write a visitor-readable Markdown chat message that is more detailed than the spoken answer. "
        "Use headings, bullets, and links when helpful. Do not duplicate the spoken wording.",
    )
    chat_rich_content_prompt: str = os.getenv("VOICEBOT_CHAT_RICH_CONTENT_PROMPT", "")
    stt_no_speech_threshold: float = env_float("VOICEBOT_STT_NO_SPEECH_THRESHOLD", 0.60)
    stt_logprob_threshold: float = env_float("VOICEBOT_STT_LOGPROB_THRESHOLD", -1.0)
    stt_min_chars: int = env_int("VOICEBOT_STT_MIN_CHARS", 2)
    stt_timeout_seconds: float = env_float("VOICEBOT_STT_TIMEOUT_SECONDS", 8.0)
    stt_partial_enabled: bool = env_bool("VOICEBOT_STT_PARTIAL_ENABLED", False)
    stt_partial_interval_seconds: float = env_float("VOICEBOT_STT_PARTIAL_INTERVAL_SECONDS", 1.0)
    stt_partial_min_seconds: float = env_float("VOICEBOT_STT_PARTIAL_MIN_SECONDS", 1.0)
    stt_partial_min_chars: int = env_int("VOICEBOT_STT_PARTIAL_MIN_CHARS", 4)
    speculative_work_enabled: bool = env_bool("VOICEBOT_SPECULATIVE_WORK_ENABLED", False)
    speculative_min_chars: int = env_int("VOICEBOT_SPECULATIVE_MIN_CHARS", 20)
    speculative_min_tokens: int = env_int("VOICEBOT_SPECULATIVE_MIN_TOKENS", 3)
    speculative_max_per_turn: int = env_int("VOICEBOT_SPECULATIVE_MAX_PER_TURN", 1)
    speculative_subagent_provider: str = os.getenv("VOICEBOT_SPECULATIVE_SUBAGENT_PROVIDER", "")
    speculative_external_intent_required: bool = env_bool("VOICEBOT_SPECULATIVE_EXTERNAL_INTENT_REQUIRED", True)
    streaming_rag_enabled: bool = env_bool("VOICEBOT_STREAMING_RAG_ENABLED", False)
    streaming_rag_trigger_mode: str = os.getenv("VOICEBOT_STREAMING_RAG_TRIGGER_MODE", "model_triggered")
    streaming_rag_max_parallel_per_turn: int = env_int("VOICEBOT_STREAMING_RAG_MAX_PARALLEL_PER_TURN", 1)
    streaming_rag_reflector_mode: str = os.getenv("VOICEBOT_STREAMING_RAG_REFLECTOR_MODE", "heuristic")
    agent_min_transcript_chars: int = env_int("VOICEBOT_AGENT_MIN_TRANSCRIPT_CHARS", 5)
    agent_min_transcript_tokens: int = env_int("VOICEBOT_AGENT_MIN_TRANSCRIPT_TOKENS", 2)
    debug_audio_capture: bool = env_bool("VOICEBOT_DEBUG_AUDIO_CAPTURE", False)
    debug_audio_dir: str = os.getenv("VOICEBOT_DEBUG_AUDIO_DIR", "/data/debug-audio")
    call_recording_enabled: bool = env_bool("VOICEBOT_CALL_RECORDING_ENABLED", True)
    call_recording_silence_threshold: float = env_float("VOICEBOT_CALL_RECORDING_SILENCE_THRESHOLD", 0.003)

    tts_provider: str = os.getenv("VOICEBOT_TTS_PROVIDER", "supertonic")
    tts_api_key: str = os.getenv("VOICEBOT_TTS_API_KEY", "")
    tts_base_url: str = os.getenv("VOICEBOT_TTS_BASE_URL", "")
    tts_model: str = os.getenv("VOICEBOT_TTS_MODEL", "")
    tts_timeout_seconds: float = env_float("VOICEBOT_TTS_TIMEOUT_SECONDS", 8.0)
    tts_voice: str = os.getenv("VOICEBOT_TTS_VOICE", "M1")
    openai_tts_model: str = os.getenv("VOICEBOT_OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
    openai_tts_voice: str = os.getenv("VOICEBOT_OPENAI_TTS_VOICE", "alloy")
    tts_cache_enabled: bool = env_bool("VOICEBOT_TTS_CACHE_ENABLED", True)
    tts_cache_dir: str = os.getenv("VOICEBOT_TTS_CACHE_DIR", "/data/tts-cache")
    tts_stream_min_chunk_seconds: float = env_float("VOICEBOT_TTS_STREAM_MIN_CHUNK_SECONDS", 0.04)
    tts_stream_max_chunk_seconds: float = env_float("VOICEBOT_TTS_STREAM_MAX_CHUNK_SECONDS", 0.30)

    start_threshold: float = env_float("VOICEBOT_START_THRESHOLD", 0.020)
    stop_threshold: float = env_float("VOICEBOT_STOP_THRESHOLD", 0.010)
    vad_start_ms: int = env_int("VOICEBOT_VAD_START_MS", 60)
    barge_in_threshold: float = env_float("VOICEBOT_BARGE_IN_THRESHOLD", 0.08)
    echo_tail_ms: int = env_int("VOICEBOT_ECHO_TAIL_MS", 300)
    silence_ms: int = env_int("VOICEBOT_SILENCE_MS", 450)
    min_seconds: float = env_float("VOICEBOT_MIN_SECONDS", 0.35)
    max_seconds: float = env_float("VOICEBOT_MAX_SECONDS", 20.0)
    max_reply_chars: int = env_int("VOICEBOT_MAX_REPLY_CHARS", 240)
    tts_chunk_chars: int = env_int("VOICEBOT_TTS_CHUNK_CHARS", 90)
    turn_coalesce_window_ms: int = env_int("VOICEBOT_TURN_COALESCE_WINDOW_MS", 250)
    turn_coalesce_max_chars: int = env_int("VOICEBOT_TURN_COALESCE_MAX_CHARS", 80)
    latency_budget_ack_seconds: float = env_float("VOICEBOT_LATENCY_BUDGET_ACK_SECONDS", 2.0)
    latency_budget_first_audio_seconds: float = env_float("VOICEBOT_LATENCY_BUDGET_FIRST_AUDIO_SECONDS", 2.5)
    latency_budget_stt_seconds: float = env_float("VOICEBOT_LATENCY_BUDGET_STT_SECONDS", 1.5)
    latency_budget_agent_seconds: float = env_float("VOICEBOT_LATENCY_BUDGET_AGENT_SECONDS", 1.2)
    latency_budget_tts_first_audio_seconds: float = env_float("VOICEBOT_LATENCY_BUDGET_TTS_FIRST_AUDIO_SECONDS", 0.8)
    latency_budget_delegated_progress_seconds: float = env_float("VOICEBOT_LATENCY_BUDGET_DELEGATED_PROGRESS_SECONDS", 12.0)
    deferred_response_wait_seconds: float = env_float("VOICEBOT_DEFERRED_RESPONSE_WAIT_SECONDS", 30.0)
    packet_ms: int = env_int("VOICEBOT_PACKET_MS", 20)
    webrtc_jitter_buffer_enabled: bool = env_bool("VOICEBOT_WEBRTC_JITTER_BUFFER_ENABLED", True)
    webrtc_jitter_target_delay_ms: int = env_int("VOICEBOT_WEBRTC_JITTER_TARGET_DELAY_MS", 60)
    webrtc_jitter_max_delay_ms: int = env_int("VOICEBOT_WEBRTC_JITTER_MAX_DELAY_MS", 200)
    audiosocket_jitter_buffer_enabled: bool = env_bool("VOICEBOT_AUDIOSOCKET_JITTER_BUFFER_ENABLED", True)
    audiosocket_jitter_target_delay_ms: int = env_int("VOICEBOT_AUDIOSOCKET_JITTER_TARGET_DELAY_MS", 60)
    audiosocket_jitter_max_delay_ms: int = env_int("VOICEBOT_AUDIOSOCKET_JITTER_MAX_DELAY_MS", 200)
    scaling_backpressure_max_inflight: int = env_int("VOICEBOT_SCALING_BACKPRESSURE_MAX_INFLIGHT", 100)

    max_context_events: int = env_int("VOICEBOT_MAX_CONTEXT_EVENTS", 80)
    relational_store_provider: str = os.getenv("VOICEBOT_RELATIONAL_STORE_PROVIDER", "").strip().lower()
    relational_database_url: str = os.getenv(
        "VOICEBOT_RELATIONAL_DATABASE_URL",
        os.getenv("DATABASE_URL", "/data/voicebot.sqlite3"),
    )
    cache_store_provider: str = os.getenv("VOICEBOT_CACHE_STORE_PROVIDER", "").strip().lower()
    redis_url: str = os.getenv("VOICEBOT_REDIS_URL", os.getenv("REDIS_URL", "redis://redis:6379/0"))
    object_storage_bucket: str = os.getenv("VOICEBOT_OBJECT_STORAGE_BUCKET", "")
    object_storage_endpoint: str = os.getenv("VOICEBOT_OBJECT_STORAGE_ENDPOINT", "")
    object_storage_region: str = os.getenv("VOICEBOT_OBJECT_STORAGE_REGION", "")
    event_store_provider: str = os.getenv("VOICEBOT_EVENT_STORE_PROVIDER", os.getenv("VOICEBOT_RELATIONAL_STORE_PROVIDER", "json")).strip().lower()
    event_store_path: str = os.getenv("VOICEBOT_EVENT_STORE_PATH", "/data/events/events.jsonl")
    agent_task_store_provider: str = os.getenv("VOICEBOT_AGENT_TASK_STORE_PROVIDER", os.getenv("VOICEBOT_CACHE_STORE_PROVIDER", "json")).strip().lower()
    agent_task_store_path: str = os.getenv("VOICEBOT_AGENT_TASK_STORE_PATH", "/data/agent_tasks.json")
    agent_task_responded_event_retention: int = env_int("VOICEBOT_AGENT_TASK_RESPONDED_EVENT_RETENTION", 10000)
    call_state_store_provider: str = os.getenv("VOICEBOT_CALL_STATE_STORE_PROVIDER", os.getenv("VOICEBOT_CACHE_STORE_PROVIDER", "json")).strip().lower()
    call_state_store_path: str = os.getenv("VOICEBOT_CALL_STATE_STORE_PATH", "/data/call_states.json")
    worker_registry_store_provider: str = os.getenv("VOICEBOT_WORKER_REGISTRY_STORE_PROVIDER", os.getenv("VOICEBOT_CACHE_STORE_PROVIDER", "json")).strip().lower()
    worker_registry_store_path: str = os.getenv("VOICEBOT_WORKER_REGISTRY_STORE_PATH", "/data/worker_registry.json")
    worker_registry_heartbeat_ttl_seconds: float = env_float("VOICEBOT_WORKER_REGISTRY_HEARTBEAT_TTL_SECONDS", 30.0)
    worker_queue_store_provider: str = os.getenv("VOICEBOT_WORKER_QUEUE_STORE_PROVIDER", os.getenv("VOICEBOT_CACHE_STORE_PROVIDER", "json")).strip().lower()
    worker_queue_store_path: str = os.getenv("VOICEBOT_WORKER_QUEUE_STORE_PATH", "/data/worker_queue.json")
    deployment_mode: str = os.getenv("VOICEBOT_DEPLOYMENT_MODE", "local").strip().lower()
    runtime_roles: tuple[str, ...] = env_csv_tuple("VOICEBOT_RUNTIME_ROLES", ("all",))
    workspace_access_control_enabled: bool = env_bool("VOICEBOT_WORKSPACE_ACCESS_CONTROL_ENABLED", False)
    allowed_workspace_ids: tuple[str, ...] = env_csv_tuple("VOICEBOT_ALLOWED_WORKSPACE_IDS")
    internal_auth_enabled: bool = env_bool("VOICEBOT_INTERNAL_AUTH_ENABLED", False)
    internal_auth_header: str = os.getenv("VOICEBOT_INTERNAL_AUTH_HEADER", "X-FlowHunt-Internal-Key")
    internal_api_keys: tuple[str, ...] = env_csv_tuple("VOICEBOT_INTERNAL_API_KEYS")
    dashboard_auth_enabled: bool = env_bool("VOICEBOT_DASHBOARD_AUTH_ENABLED", False)
    dashboard_dev_login_enabled: bool = env_bool("VOICEBOT_DASHBOARD_DEV_LOGIN_ENABLED", False)
    dashboard_user_id_header: str = os.getenv("VOICEBOT_DASHBOARD_USER_ID_HEADER", "X-FlowHunt-User-Id")
    dashboard_workspace_ids_header: str = os.getenv("VOICEBOT_DASHBOARD_WORKSPACE_IDS_HEADER", "X-FlowHunt-Workspace-Ids")
    default_workspace_id: str = (
        os.getenv("VOICEBOT_DEFAULT_WORKSPACE_ID")
        or os.getenv("FLOWHUNT_WORKSPACE_ID")
        or os.getenv("VOICEBOT_FLOWHUNT_WORKSPACE_ID")
        or ""
    ).strip()
    default_voicebot_id: str = os.getenv("VOICEBOT_DEFAULT_VOICEBOT_ID", "default").strip()
    default_voicebot_display_name: str = os.getenv("VOICEBOT_DEFAULT_VOICEBOT_DISPLAY_NAME", "Default Voicebot").strip()
    public_session_rate_limit_per_minute: int = env_int("VOICEBOT_PUBLIC_SESSION_RATE_LIMIT_PER_MINUTE", 60)
    public_voicebot_max_concurrent_sessions: int = env_int("VOICEBOT_PUBLIC_VOICEBOT_MAX_CONCURRENT_SESSIONS", 100)
    public_sdp_max_bytes: int = env_int("VOICEBOT_PUBLIC_SDP_MAX_BYTES", 131072)
    pii_safe_logging_enabled: bool = env_bool("VOICEBOT_PII_SAFE_LOGGING_ENABLED", True)
    transcript_dir: str = os.getenv("VOICEBOT_TRANSCRIPT_DIR", "/data/transcripts")
    transcript_store_provider: str = os.getenv("VOICEBOT_TRANSCRIPT_STORE_PROVIDER", os.getenv("VOICEBOT_RELATIONAL_STORE_PROVIDER", "jsonl")).strip().lower()
    provider_config_store_provider: str = os.getenv("VOICEBOT_PROVIDER_CONFIG_STORE_PROVIDER", os.getenv("VOICEBOT_RELATIONAL_STORE_PROVIDER", "json")).strip().lower()
    provider_config_store_path: str = os.getenv("VOICEBOT_PROVIDER_CONFIG_STORE_PATH", "/data/provider_config.json")
    voicebot_session_store_provider: str = os.getenv("VOICEBOT_SESSION_STORE_PROVIDER", os.getenv("VOICEBOT_RELATIONAL_STORE_PROVIDER", "json")).strip().lower()
    voicebot_session_store_path: str = os.getenv("VOICEBOT_SESSION_STORE_PATH", "/data/voicebot_sessions.json")
    session_lease_store_provider: str = os.getenv("VOICEBOT_SESSION_LEASE_STORE_PROVIDER", os.getenv("VOICEBOT_CACHE_STORE_PROVIDER", "json")).strip().lower()
    session_lease_store_path: str = os.getenv("VOICEBOT_SESSION_LEASE_STORE_PATH", "/data/session_leases.json")
    sip_trunk_store_provider: str = os.getenv("VOICEBOT_SIP_TRUNK_STORE_PROVIDER", os.getenv("VOICEBOT_RELATIONAL_STORE_PROVIDER", "json")).strip().lower()
    sip_trunk_registry_path: str = os.getenv("VOICEBOT_SIP_TRUNK_REGISTRY_PATH", "/data/sip_trunks.json")
    sip_trunk_pjsip_include_path: str = os.getenv(
        "VOICEBOT_SIP_TRUNK_PJSIP_INCLUDE_PATH",
        "/data/asterisk/pjsip-trunks.conf",
    )
    audio_artifact_store_provider: str = os.getenv("VOICEBOT_AUDIO_ARTIFACT_STORE_PROVIDER", "filesystem").strip().lower()
    greet_on_connect: bool = env_bool("VOICEBOT_GREET_ON_CONNECT", True)
    connect_greeting_prompt: str = os.getenv(
        "VOICEBOT_CONNECT_GREETING_PROMPT",
        "Hello, how can I help you?",
    )

    ami_host: str = os.getenv("VOICEBOT_AMI_HOST", "asterisk")
    ami_port: int = env_int("VOICEBOT_AMI_PORT", 5038)
    ami_username: str = os.getenv("VOICEBOT_AMI_USERNAME", "voicebot")
    ami_password: str = os.getenv("VOICEBOT_AMI_PASSWORD", "")

    flowhunt_api_key: str = os.getenv("FLOWHUNT_API_KEY", os.getenv("VOICEBOT_FLOWHUNT_API_KEY", ""))
    flowhunt_workspace_id: str = os.getenv("FLOWHUNT_WORKSPACE_ID", os.getenv("VOICEBOT_FLOWHUNT_WORKSPACE_ID", ""))
    flowhunt_base_url: str = os.getenv("VOICEBOT_FLOWHUNT_BASE_URL", "https://api.flowhunt.io")
    flowhunt_timeout: float = env_float("VOICEBOT_FLOWHUNT_TIMEOUT", 30.0)
    flowhunt_complex_backend: str = os.getenv("VOICEBOT_FLOWHUNT_COMPLEX_BACKEND", "project").strip().lower()
    flowhunt_project_id: str = os.getenv("FLOWHUNT_PROJECT_ID", os.getenv("VOICEBOT_FLOWHUNT_PROJECT_ID", ""))
    flowhunt_flow_id: str = os.getenv("FLOWHUNT_FLOW_ID", os.getenv("VOICEBOT_FLOWHUNT_FLOW_ID", ""))
    flowhunt_issue_wait_seconds: float = env_float("VOICEBOT_FLOWHUNT_ISSUE_WAIT_SECONDS", 45.0)
    flowhunt_issue_poll_interval_seconds: float = env_float("VOICEBOT_FLOWHUNT_ISSUE_POLL_INTERVAL_SECONDS", 2.0)
    flowhunt_progress_update_seconds: float = env_float("VOICEBOT_FLOWHUNT_PROGRESS_UPDATE_SECONDS", 12.0)
    flowhunt_issue_background_wait_seconds: float = env_float("VOICEBOT_FLOWHUNT_ISSUE_BACKGROUND_WAIT_SECONDS", 600.0)
    flowhunt_flow_wait_seconds: float = env_float("VOICEBOT_FLOWHUNT_FLOW_WAIT_SECONDS", 0.0)
    flowhunt_flow_poll_interval_seconds: float = env_float("VOICEBOT_FLOWHUNT_FLOW_POLL_INTERVAL_SECONDS", 3.0)
    subagent_task_store_provider: str = os.getenv("VOICEBOT_SUBAGENT_TASK_STORE_PROVIDER", os.getenv("VOICEBOT_CACHE_STORE_PROVIDER", "json")).strip().lower()
    subagent_task_store_path: str = os.getenv("VOICEBOT_SUBAGENT_TASK_STORE_PATH", "/data/subagent_tasks.json")
    subagent_task_poll_loop_seconds: float = env_float("VOICEBOT_SUBAGENT_TASK_POLL_LOOP_SECONDS", 1.0)
    subagent_task_initial_poll_seconds: float = env_float("VOICEBOT_SUBAGENT_TASK_INITIAL_POLL_SECONDS", 3.0)
    subagent_task_max_poll_seconds: float = env_float("VOICEBOT_SUBAGENT_TASK_MAX_POLL_SECONDS", 30.0)
    subagent_task_timeout_seconds: float = env_float("VOICEBOT_SUBAGENT_TASK_TIMEOUT_SECONDS", 600.0)
    subagent_task_max_attempts: int = env_int("VOICEBOT_SUBAGENT_TASK_MAX_ATTEMPTS", 100)
    subagent_providers: tuple[dict[str, Any], ...] = env_json_list_any(
        ("VOICEBOT_SUBAGENT_PROVIDERS", "VOICEBOT_HTTP_SUBAGENT_PROVIDERS"),
        [],
    )
    http_subagent_providers: tuple[dict[str, Any], ...] = env_json_list("VOICEBOT_HTTP_SUBAGENT_PROVIDERS", [])

    stt_pipeline: tuple[dict[str, Any], ...] = env_json_list(
        "VOICEBOT_STT_PIPELINE",
        [{"name": "stt"}, {"name": "agent-request"}],
    )
    tts_pipeline: tuple[dict[str, Any], ...] = env_json_list(
        "VOICEBOT_TTS_PIPELINE",
        [{"name": "tts"}],
    )


SENSITIVE_FIELD_MARKERS = ("api_key", "password", "secret", "token")


def redacted_settings(settings: Settings) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in fields(settings):
        value = getattr(settings, field.name)
        if is_sensitive_field(field.name):
            result[field.name] = {
                "configured": bool(value),
                "redacted": True,
            }
        elif field.name in {"subagent_providers", "http_subagent_providers"}:
            result[field.name] = [redact_http_subagent_provider(item) for item in value]
        else:
            result[field.name] = json_safe_value(value)
    return result


def is_sensitive_field(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in SENSITIVE_FIELD_MARKERS)


def json_safe_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [json_safe_value(item) for item in value]
    if isinstance(value, list):
        return [json_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe_value(item) for key, item in value.items()}
    return value


def redact_http_subagent_provider(provider: dict[str, Any]) -> dict[str, Any]:
    redacted = json_safe_value(provider)
    if isinstance(redacted, dict) and redacted.get("headers"):
        redacted["headers"] = {"configured": True, "redacted": True}
    return redacted
