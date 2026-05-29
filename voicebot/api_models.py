from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AgentResponseRequest(BaseModel):
    text: str
    response_to_event_id: int | None = None


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


class WorkerQueueClaimRequest(BaseModel):
    queue: str
    owner: str
    limit: int = 1
    ttl_seconds: float = 30.0


class WorkerQueueItemRequest(BaseModel):
    item_id: str
    owner: str | None = None


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
    secret_ref: SecretReferenceRequest | None = None
    fallback_provider: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class VoicebotProviderConfigRequest(BaseModel):
    stt: ProviderChoiceRequest
    tts: ProviderChoiceRequest
    agent: ProviderChoiceRequest


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


class SessionLeaseReleaseRequest(BaseModel):
    workspace_id: str
    voicebot_id: str
    session_id: str
    owner: str | None = None


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
