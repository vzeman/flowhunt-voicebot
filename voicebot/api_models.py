from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AgentResponseRequest(BaseModel):
    text: str
    response_to_event_id: int | None = None
    response_kind: str | None = None
    partial: bool = False
    finalize_only: bool = False
    chat: dict[str, Any] | None = None


class CallMessageRequest(BaseModel):
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SpeculativeSubagentTaskRequest(BaseModel):
    workspace_id: str
    session_id: str
    request_event_id: int
    provider: str = "flowhunt_flow"
    input_text: str
    voicebot_id: str | None = None
    dedupe_key: str | None = None
    speculative_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SpeculativeSubagentConfirmRequest(BaseModel):
    workspace_id: str
    final_request_event_id: int
    final_input_text: str
    notify_if_terminal: bool = True


class SpeculativeSubagentCancelRequest(BaseModel):
    workspace_id: str
    reason: str = "final_transcript_changed"


class CompactContextRequest(BaseModel):
    summary: str
    call_id: str = "system"


class ConversationEvaluationRequest(BaseModel):
    call_id: str | None = None
    workspace_id: str | None = None
    voicebot_id: str | None = None
    session_id: str | None = None
    after: int = 0
    limit: int = 1000
    must_include_event_types: list[str] = Field(default_factory=list)
    max_duplicate_agent_responses: int = 1
    require_final_agent_response: bool = False


class ScalingWorkloadPlanRequest(BaseModel):
    workspace_id: str
    voicebot_id: str
    concurrent_sessions: int = 0
    session_id: str | None = None
    stt_provider: str | None = None
    tts_provider: str | None = None
    agent_provider: str | None = None
    baseline_sessions: int = 0
    call_growth_per_minute: float = 0.0
    worker_warmup_seconds: float = 30.0
    max_concurrent_sessions: int = 100
    burst_sessions: int = 0
    scale_to_zero_allowed: bool = False


class ScalingAdmissionRequest(BaseModel):
    workspace_id: str
    voicebot_id: str
    max_concurrent_sessions: int = 100
    burst_sessions: int = 0
    scale_to_zero_allowed: bool = False


class IncomingSessionAdmissionRequest(BaseModel):
    channel_kind: str
    external_id: str
    session_id: str
    owner: str
    transport: str
    call_id: str | None = None
    acquire_lease: bool = True
    lease_ttl_seconds: float = 30.0
    max_concurrent_sessions: int = 100
    burst_sessions: int = 0


class DrainRequest(BaseModel):
    reason: str = "operator_requested"
    interrupt_active_sessions: bool = False


class SecurityAuditRequest(BaseModel):
    action: str
    actor: str = "api"
    voicebot_id: str | None = None
    session_id: str | None = None
    call_id: str | None = None
    resource_type: str = ""
    resource_id: str | None = None
    outcome: str = "requested"
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetentionDeleteRequest(BaseModel):
    voicebot_id: str | None = None
    session_id: str | None = None
    call_id: str | None = None
    artifact_id: str | None = None
    classes: list[str] = Field(default_factory=list)
    reason: str = "operator_request"
    dry_run: bool = True


class SubagentTaskSubmitRequest(BaseModel):
    workspace_id: str
    session_id: str
    request_event_id: int
    provider: str
    input_text: str
    voicebot_id: str | None = None
    dedupe_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    schedule: bool = True


class SubagentTaskCancelRequest(BaseModel):
    workspace_id: str


class ScalingBackpressureRequest(BaseModel):
    workspace_id: str
    voicebot_id: str
    session_id: str | None = None
    provider: str | None = None


class WorkerHeartbeatRequest(BaseModel):
    worker_id: str
    role: str
    queue: str
    workspace_id: str | None = None
    voicebot_id: str | None = None
    capacity: int = 1
    status: str = "active"


class WorkerQueueRoutingRequest(BaseModel):
    workspace_id: str
    voicebot_id: str
    session_id: str | None = None
    provider: str | None = None


class WorkerQueueEnqueueRequest(BaseModel):
    item_id: str
    kind: str
    routing: WorkerQueueRoutingRequest
    queue: str
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None
    created_at: str | None = None
    attempt: int = 0
    idempotency_key: str | None = None
    max_attempts: int = 3
    priority: str | None = None


class WorkerQueueClaimRequest(BaseModel):
    queue: str
    owner: str
    limit: int = 1
    ttl_seconds: float = 30.0


class WorkerQueueItemRequest(BaseModel):
    item_id: str
    owner: str | None = None
    ttl_seconds: float = 30.0
    error: str | None = None


class MultimodalContentRequest(BaseModel):
    modality: str
    direction: str
    mime_type: str | None = None
    uri: str | None = None
    text: str | None = None
    workspace_id: str | None = None
    voicebot_id: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SecretReferenceRequest(BaseModel):
    name: str
    workspace_id: str | None = None


class ProviderChoiceRequest(BaseModel):
    provider: str
    model: str | None = None
    voice: str | None = None
    secret_ref: SecretReferenceRequest | None = None
    fallback_provider: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class VoicebotProviderConfigRequest(BaseModel):
    stt: ProviderChoiceRequest
    tts: ProviderChoiceRequest
    agent: ProviderChoiceRequest


class VoicebotChatPromptConfigRequest(BaseModel):
    mode: str = "disabled"
    system_prompt: str = ""
    response_prompt: str = (
        "When chat is enabled, write a visitor-readable chat message that is more detailed than the spoken answer. "
        "Do not duplicate the spoken wording."
    )
    rich_content_prompt: str = ""


class VoicebotChatPromptConfigPatchRequest(BaseModel):
    mode: str | None = None
    system_prompt: str | None = None
    response_prompt: str | None = None
    rich_content_prompt: str | None = None


class VoicebotPromptConfigRequest(BaseModel):
    greeting: str = "Hello, how can I help you?"
    filler_message: str = "Give me a moment."
    colleague_progress_message: str = (
        "I asked a colleague to check that. I will tell you the result as soon as it is ready."
    )
    system_prompt: str = ""
    stt_prompt: str = ""
    language: str = "en"
    chat: VoicebotChatPromptConfigRequest = Field(default_factory=VoicebotChatPromptConfigRequest)


class VoicebotPromptConfigPatchRequest(BaseModel):
    greeting: str | None = None
    filler_message: str | None = None
    colleague_progress_message: str | None = None
    system_prompt: str | None = None
    stt_prompt: str | None = None
    language: str | None = None
    chat: VoicebotChatPromptConfigPatchRequest | None = None


class VoicebotRealtimeConfigRequest(BaseModel):
    silence_ms: int = 450
    vad_start_ms: int = 60
    min_seconds: float = 0.35
    max_seconds: float = 20.0
    start_threshold: float = 0.020
    stop_threshold: float = 0.010
    barge_in_threshold: float = 0.08
    echo_tail_ms: int = 300
    max_reply_chars: int = 240
    tts_chunk_chars: int = 90


class VoicebotChannelConfigRequest(BaseModel):
    voice_enabled: bool = True
    chat_enabled: bool = False
    chat_input_enabled: bool = False
    transcript_visible: bool = False
    rich_content_enabled: bool = False


class VoicebotQuotaConfigRequest(BaseModel):
    max_concurrent_sessions: int = 1
    max_provider_inflight: int = 10
    enabled_actions: list[str] = Field(
        default_factory=lambda: ["say", "hangup_call", "transfer_call", "send_dtmf", "invoke_flowhunt_flow"]
    )


class SubagentPromptConfigRequest(BaseModel):
    before_call_prompt: str = "I will ask a colleague to check that and come back with the result."
    after_call_prompt: str = "A colleague is working on the request."
    result_prompt: str = "A colleague finished checking the caller request. Result: {result}"


class VoicebotSubagentConfigRequest(BaseModel):
    flowhunt_workspace_id: str = ""
    flowhunt_flow_id: str = ""
    flowhunt_project_id: str = ""
    complex_backend: str = "flow"
    prompts: dict[str, SubagentPromptConfigRequest] = Field(default_factory=dict)


class VoicebotRuntimeConfigRequest(BaseModel):
    providers: VoicebotProviderConfigRequest
    prompts: VoicebotPromptConfigRequest = Field(default_factory=VoicebotPromptConfigRequest)
    realtime: VoicebotRealtimeConfigRequest = Field(default_factory=VoicebotRealtimeConfigRequest)
    channels: VoicebotChannelConfigRequest = Field(default_factory=VoicebotChannelConfigRequest)
    quotas: VoicebotQuotaConfigRequest = Field(default_factory=VoicebotQuotaConfigRequest)
    subagents: VoicebotSubagentConfigRequest = Field(default_factory=VoicebotSubagentConfigRequest)
    enabled: bool = True


class VoicebotAdminRequest(BaseModel):
    voicebot_id: str
    display_name: str = ""
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class VoicebotAdminPatchRequest(BaseModel):
    display_name: str | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


class VoicebotChannelRequest(BaseModel):
    channel_id: str
    kind: str
    external_id: str
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class VoicebotChannelPatchRequest(BaseModel):
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


class PublicVoicebotRouteRequest(BaseModel):
    route_id: str
    channel_id: str
    host: str
    path_prefix: str = "/"
    status: str = "pending"
    tls_mode: str = "managed"
    allowed_origins: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PublicVoicebotRoutePatchRequest(BaseModel):
    channel_id: str | None = None
    host: str | None = None
    path_prefix: str | None = None
    status: str | None = None
    tls_mode: str | None = None
    allowed_origins: list[str] | None = None
    metadata: dict[str, Any] | None = None


class CallControlRequest(BaseModel):
    action: str
    target: str | None = None
    digit: str | None = None
    response_to_event_id: int | None = None


class SipTrunkRequest(BaseModel):
    trunk_id: str
    host: str
    user: str
    password: str
    auth_user: str = ""
    contact_user: str = ""
    from_user: str = ""
    display_name: str = ""
    enabled: bool = True
    codecs: list[str] = Field(default_factory=lambda: ["ulaw", "alaw", "slin"])
    expiration: int = 300
    retry_interval: int = 30
    forbidden_retry_interval: int = 300


class WebRTCOfferRequest(BaseModel):
    sdp: str
    type: str = "offer"
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlaybackInterruptRequest(BaseModel):
    reason: str = "agent_requested"
    response_to_event_id: int | None = None


class SessionLeaseRequest(BaseModel):
    workspace_id: str
    voicebot_id: str
    session_id: str
    owner: str
    ttl_seconds: float = 30.0
    call_id: str | None = None
    transport: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionLeaseReleaseRequest(BaseModel):
    workspace_id: str
    voicebot_id: str
    session_id: str
    owner: str | None = None


class SessionLeaseEnforceRequest(BaseModel):
    owner: str
    stop_unleased_sessions: bool = True
    recover_non_media_work: bool = True
    reacquire_missing_leases: bool = True
    lease_ttl_seconds: float = 30.0


class AgentToolRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentTaskClaimRequest(BaseModel):
    event_ids: list[int]
    owner: str = "agent"
    ttl_seconds: float = 60.0


class AgentTaskReleaseRequest(BaseModel):
    event_ids: list[int]
    owner: str | None = None


class AgentTaskRenewRequest(BaseModel):
    event_ids: list[int]
    owner: str
    ttl_seconds: float = 60.0
