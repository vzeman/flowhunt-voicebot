from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, get_args

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from .api_models import (
    IncomingSessionAdmissionRequest,
    ScalingAdmissionRequest,
    ScalingBackpressureRequest,
    ScalingWorkloadPlanRequest,
    SessionLeaseEnforceRequest,
    SessionLeaseReleaseRequest,
    SessionLeaseRequest,
    WorkerHeartbeatRequest,
    WorkerQueueClaimRequest,
    WorkerQueueEnqueueRequest,
    WorkerQueueItemRequest,
)
from .events import event_to_dict
from .routing_admission import IncomingSessionRequest, evaluate_incoming_session
from .scaling import (
    RoutingKey,
    WorkerInstance,
    WorkerQueueEnvelope,
    WorkerRole,
    WorkloadProfile,
    WarmCapacityPolicy,
    admission_decision,
    autoscaling_signals,
    autoscaling_signals_prometheus,
    build_workload_plan,
    default_deployment_topology,
    default_work_priority,
    priority_routing_rules,
)
from .session_ownership import audit_session_ownership
from .workspace_model import ChannelKind


@dataclass(frozen=True)
class ScalingApiContext:
    events: Any
    registry: Any
    worker_registry: Any
    worker_queue: Any
    backpressure: Any
    session_lease_store: Any
    channel_resolver: Any
    voicebot_store: Any
    provider_config_store: Any
    runtime_config_store: Any
    workspace_access_policy: Any


def create_scaling_router(context: ScalingApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/scaling/topology")
    def scaling_topology() -> dict[str, Any]:
        return default_deployment_topology().as_dict()

    @router.post("/scaling/workload-plan")
    def scaling_workload_plan(request: ScalingWorkloadPlanRequest) -> dict[str, Any]:
        try:
            profile = WorkloadProfile(
                workspace_id=request.workspace_id,
                voicebot_id=request.voicebot_id,
                concurrent_sessions=request.concurrent_sessions,
                session_id=request.session_id,
                stt_provider=request.stt_provider,
                tts_provider=request.tts_provider,
                agent_provider=request.agent_provider,
                baseline_sessions=request.baseline_sessions,
                call_growth_per_minute=request.call_growth_per_minute,
                worker_warmup_seconds=request.worker_warmup_seconds,
                max_concurrent_sessions=request.max_concurrent_sessions,
                burst_sessions=request.burst_sessions,
                scale_to_zero_allowed=request.scale_to_zero_allowed,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return build_workload_plan(profile)

    @router.get("/scaling/signals")
    def scaling_signals(
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
        format: str = "json",
    ):
        signals = autoscaling_signals(
            active_session_snapshots=context.registry.snapshots(),
            worker_registry=context.worker_registry,
            worker_queue=context.worker_queue,
            events=context.events.list_events(limit=1000),
            workspace_id=workspace_id,
            voicebot_id=voicebot_id,
        )
        if format == "prometheus":
            return PlainTextResponse(autoscaling_signals_prometheus(signals), media_type="text/plain; version=0.0.4")
        if format != "json":
            raise HTTPException(status_code=400, detail="format must be json or prometheus")
        return signals

    @router.post("/scaling/admission")
    def scaling_admission(request: ScalingAdmissionRequest) -> dict[str, Any]:
        try:
            policy = WarmCapacityPolicy(
                max_concurrent_sessions=request.max_concurrent_sessions,
                burst_sessions=request.burst_sessions,
                scale_to_zero_allowed=request.scale_to_zero_allowed,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        decision = admission_decision(
            active_session_snapshots=context.registry.snapshots(),
            workspace_id=request.workspace_id,
            voicebot_id=request.voicebot_id,
            policy=policy,
        )
        if not decision["allowed"]:
            context.events.append(
                request.workspace_id,
                "metrics",
                {
                    "name": "capacity_rejection",
                    "value": 1.0,
                    "workspace_id": request.workspace_id,
                    "voicebot_id": request.voicebot_id,
                    "reason": decision["reason"],
                },
            )
        return decision

    @router.post("/routing/admission")
    def routing_admission(request: IncomingSessionAdmissionRequest) -> dict[str, Any]:
        try:
            admission_request = IncomingSessionRequest(
                channel_kind=_validated_channel_kind(request.channel_kind),
                external_id=request.external_id,
                session_id=request.session_id,
                owner=request.owner,
                transport=request.transport,
                call_id=request.call_id,
                acquire_lease=request.acquire_lease,
                lease_ttl_seconds=request.lease_ttl_seconds,
                max_concurrent_sessions=request.max_concurrent_sessions,
                burst_sessions=request.burst_sessions,
            )
            decision = evaluate_incoming_session(
                admission_request,
                channel_resolver=context.channel_resolver,
                voicebot_store=context.voicebot_store,
                provider_config_store=context.provider_config_store,
                runtime_config_store=context.runtime_config_store,
                workspace_access_policy=context.workspace_access_policy,
                session_lease_store=context.session_lease_store,
                active_session_snapshots=context.registry.snapshots(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        event = context.events.append(
            decision.get("call_id") or decision.get("session_id") or request.external_id,
            "session_admission_decided",
            {key: value for key, value in decision.items() if key not in {"lease"}},
        )
        if not decision["allowed"]:
            context.events.append(
                decision.get("workspace_id") or request.external_id,
                "metrics",
                {
                    "name": "capacity_rejection",
                    "value": 1.0,
                    "reason": decision["reason"],
                    "workspace_id": decision.get("workspace_id"),
                    "voicebot_id": decision.get("voicebot_id"),
                    "transport": request.transport,
                },
            )
        return {"event_id": event.id, **decision}

    @router.post("/scaling/workers/heartbeat")
    def scaling_worker_heartbeat(request: WorkerHeartbeatRequest) -> dict[str, Any]:
        try:
            worker = context.worker_registry.heartbeat(
                WorkerInstance(
                    worker_id=request.worker_id,
                    role=_validated_worker_role(request.role),
                    queue=request.queue,
                    workspace_id=request.workspace_id,
                    voicebot_id=request.voicebot_id,
                    capacity=request.capacity,
                    status=_validated_worker_status(request.status),
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"worker": worker.as_dict()}

    @router.get("/scaling/workers")
    def scaling_worker_list(
        role: str | None = None,
        workspace_id: str | None = None,
        voicebot_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            worker_role = _validated_worker_role(role) if role else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        workers = context.worker_registry.active(role=worker_role, workspace_id=workspace_id, voicebot_id=voicebot_id)
        return {"workers": [worker.as_dict() for worker in workers]}

    @router.post("/scaling/workers/{worker_id}/drain")
    def scaling_worker_drain(worker_id: str) -> dict[str, Any]:
        try:
            worker = context.worker_registry.mark_draining(worker_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="worker not found") from None
        return {"worker": worker.as_dict()}

    @router.delete("/scaling/workers/{worker_id}")
    def scaling_worker_remove(worker_id: str) -> dict[str, Any]:
        return {"removed": context.worker_registry.remove(worker_id)}

    @router.get("/scaling/capacity")
    def scaling_capacity(workspace_id: str | None = None, voicebot_id: str | None = None) -> dict[str, Any]:
        return context.worker_registry.capacity_summary(workspace_id=workspace_id, voicebot_id=voicebot_id)

    @router.get("/scaling/backpressure")
    def scaling_backpressure_snapshot() -> dict[str, Any]:
        return context.backpressure.snapshot()

    @router.get("/scaling/session-leases")
    def scaling_session_lease_snapshot(workspace_id: str | None = None, voicebot_id: str | None = None) -> dict[str, Any]:
        return {
            "leases": [
                lease.as_dict()
                for lease in context.session_lease_store.list(workspace_id=workspace_id, voicebot_id=voicebot_id)
            ]
        }

    @router.get("/scaling/session-ownership")
    def scaling_session_ownership(expected_owner: str | None = None) -> dict[str, Any]:
        rows = audit_session_ownership(context.registry.snapshots(), context.session_lease_store, expected_owner=expected_owner)
        return {
            "expected_owner": expected_owner,
            "sessions": rows,
            "summary": {
                "total": len(rows),
                "owned": sum(1 for row in rows if row["status"] == "owned"),
                "missing": sum(1 for row in rows if row["status"] == "missing"),
                "owner_mismatch": sum(1 for row in rows if row["status"] == "owner_mismatch"),
                "unscoped": sum(1 for row in rows if row["status"] == "unscoped"),
            },
        }

    @router.post("/scaling/session-leases/acquire")
    def scaling_session_lease_acquire(request: SessionLeaseRequest) -> dict[str, Any]:
        try:
            lease = context.session_lease_store.acquire(
                request.workspace_id,
                request.voicebot_id,
                request.session_id,
                request.owner,
                request.ttl_seconds,
                call_id=request.call_id,
                transport=request.transport,
                metadata=request.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if lease is not None:
            context.events.append(lease.call_id or lease.session_id, "session_lease_acquired", lease.as_dict())
        return {"acquired": lease is not None, "lease": lease.as_dict() if lease is not None else None}

    @router.post("/scaling/session-leases/renew")
    def scaling_session_lease_renew(request: SessionLeaseRequest) -> dict[str, Any]:
        try:
            lease = context.session_lease_store.renew(
                request.workspace_id,
                request.voicebot_id,
                request.session_id,
                request.owner,
                request.ttl_seconds,
                call_id=request.call_id,
                transport=request.transport,
                metadata=request.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if lease is not None:
            context.events.append(lease.call_id or lease.session_id, "session_lease_renewed", lease.as_dict())
        return {"renewed": lease is not None, "lease": lease.as_dict() if lease is not None else None}

    @router.post("/scaling/session-leases/release")
    def scaling_session_lease_release(request: SessionLeaseReleaseRequest) -> dict[str, Any]:
        lease = context.session_lease_store.release(
            request.workspace_id,
            request.voicebot_id,
            request.session_id,
            owner=request.owner,
        )
        if lease is not None:
            context.events.append(lease.call_id or lease.session_id, "session_lease_released", lease.as_dict())
        return {"released": lease is not None, "lease": lease.as_dict() if lease is not None else None}

    @router.post("/scaling/session-leases/expire")
    def scaling_session_lease_expire() -> dict[str, Any]:
        expired = context.session_lease_store.expire()
        for lease in expired:
            context.events.append(lease.call_id or lease.session_id, "session_lease_expired", lease.as_dict())
        return {"expired": [lease.as_dict() for lease in expired]}

    @router.post("/scaling/session-leases/enforce")
    def scaling_session_lease_enforce(request: SessionLeaseEnforceRequest) -> dict[str, Any]:
        expired = context.session_lease_store.expire()
        for lease in expired:
            context.events.append(lease.call_id or lease.session_id, "session_lease_expired", lease.as_dict())
        interrupted = []
        recovered = []
        for row in audit_session_ownership(context.registry.snapshots(), context.session_lease_store, expected_owner=request.owner):
            if row["status"] in {"owned", "unscoped"}:
                continue
            loss_data = {
                "workspace_id": row["workspace_id"],
                "voicebot_id": row["voicebot_id"],
                "session_id": row["session_id"],
                "call_id": row["call_id"],
                "transport": row["transport"],
                "expected_owner": request.owner,
                "current_owner": row["current_owner"],
                "reason": row["reason"],
            }
            context.events.append(row["call_id"], "session_lease_lost", loss_data)
            reacquired_lease = None
            if request.reacquire_missing_leases and row["status"] == "missing":
                reacquired_lease = context.session_lease_store.acquire(
                    row["workspace_id"],
                    row["voicebot_id"],
                    row["session_id"],
                    request.owner,
                    request.lease_ttl_seconds,
                    call_id=row["call_id"],
                    transport=row["transport"],
                    metadata={"recovered_from": row["reason"]},
                )
                if reacquired_lease is not None:
                    context.events.append(row["call_id"], "session_lease_reacquired", reacquired_lease.as_dict())
            if request.recover_non_media_work:
                recovered_event = context.events.append(
                    row["call_id"],
                    "session_recovered",
                    {
                        **loss_data,
                        "recovered_work": ["subagent_polling", "transcript_storage", "late_task_results"],
                        "reacquired_lease": reacquired_lease.as_dict() if reacquired_lease is not None else None,
                    },
                )
                recovered.append(event_to_dict(recovered_event))
            if request.stop_unleased_sessions:
                stopped = context.registry.stop(row["call_id"])
                interrupted_event = context.events.append(row["call_id"], "session_interrupted", {**loss_data, "stopped": stopped})
                interrupted.append(event_to_dict(interrupted_event))
        return {
            "expired": [lease.as_dict() for lease in expired],
            "recovered": recovered,
            "interrupted": interrupted,
        }

    @router.post("/scaling/backpressure/acquire")
    def scaling_backpressure_acquire(request: ScalingBackpressureRequest) -> dict[str, Any]:
        try:
            key = _backpressure_key_from_request(request)
            acquired = context.backpressure.acquire(key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"acquired": acquired, "key": key, **context.backpressure.snapshot()}

    @router.post("/scaling/backpressure/release")
    def scaling_backpressure_release(request: ScalingBackpressureRequest) -> dict[str, Any]:
        try:
            key = _backpressure_key_from_request(request)
            context.backpressure.release(key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"released": True, "key": key, **context.backpressure.snapshot()}

    @router.get("/scaling/queue")
    def scaling_queue_snapshot() -> dict[str, Any]:
        return context.worker_queue.snapshot()

    @router.post("/scaling/queue/enqueue")
    def scaling_queue_enqueue(request: WorkerQueueEnqueueRequest) -> dict[str, Any]:
        try:
            envelope = context.worker_queue.enqueue(
                WorkerQueueEnvelope(
                    item_id=request.item_id,
                    kind=request.kind,  # type: ignore[arg-type]
                    routing=RoutingKey(
                        workspace_id=request.routing.workspace_id,
                        voicebot_id=request.routing.voicebot_id,
                        session_id=request.routing.session_id,
                        provider=request.routing.provider,
                    ),
                    queue=request.queue,
                    payload=request.payload,
                    trace_id=request.trace_id,
                    created_at=request.created_at or datetime.now().astimezone().isoformat(),
                    attempt=request.attempt,
                    idempotency_key=request.idempotency_key,
                    max_attempts=request.max_attempts,
                    priority=request.priority or default_work_priority(request.kind, request.payload),
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"item": envelope.as_dict()}

    @router.post("/scaling/queue/claim")
    def scaling_queue_claim(request: WorkerQueueClaimRequest) -> dict[str, Any]:
        try:
            claimed = context.worker_queue.claim(
                request.queue,
                request.owner,
                limit=request.limit,
                ttl_seconds=request.ttl_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"items": [item.as_dict() for item in claimed]}

    @router.get("/scaling/queue/priorities")
    def scaling_queue_priorities() -> dict[str, Any]:
        return priority_routing_rules()

    @router.post("/scaling/queue/renew")
    def scaling_queue_renew(request: WorkerQueueItemRequest) -> dict[str, Any]:
        if request.owner is None:
            raise HTTPException(status_code=400, detail="owner is required")
        try:
            item = context.worker_queue.renew(request.item_id, request.owner, ttl_seconds=request.ttl_seconds)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if item is None:
            raise HTTPException(status_code=404, detail="work item claim not found")
        return {"item": item.as_dict(), "renewed": True}

    @router.post("/scaling/queue/ack")
    def scaling_queue_ack(request: WorkerQueueItemRequest) -> dict[str, Any]:
        item = context.worker_queue.ack(request.item_id, owner=request.owner)
        if item is None:
            raise HTTPException(status_code=404, detail="work item claim not found")
        return {"item": item.as_dict(), "acked": True}

    @router.post("/scaling/queue/release")
    def scaling_queue_release(request: WorkerQueueItemRequest) -> dict[str, Any]:
        item = context.worker_queue.release(request.item_id, owner=request.owner, error=request.error)
        if item is None:
            raise HTTPException(status_code=404, detail="work item claim not found")
        return {"item": item.as_dict(), "released": item.failed_at is None, "dead_lettered": item.failed_at is not None}

    @router.get("/scaling/queue/dead-letter")
    def scaling_queue_dead_letter() -> dict[str, Any]:
        return {"items": [item.as_dict() for item in context.worker_queue.dead_lettered()]}

    return router


def _validated_worker_role(value: str) -> WorkerRole:
    if value not in get_args(WorkerRole):
        raise ValueError(f"unsupported worker role: {value}")
    return value  # type: ignore[return-value]


def _validated_worker_status(value: str):
    if value not in {"active", "draining"}:
        raise ValueError(f"unsupported worker status: {value}")
    return value


def _validated_channel_kind(value: str) -> ChannelKind:
    if value not in get_args(ChannelKind):
        raise ValueError(f"unsupported channel kind: {value}")
    return value  # type: ignore[return-value]


def _backpressure_key_from_request(request: ScalingBackpressureRequest) -> str:
    routing = RoutingKey(
        workspace_id=request.workspace_id,
        voicebot_id=request.voicebot_id,
        session_id=request.session_id,
        provider=request.provider,
    )
    return routing.provider_key() if request.provider else routing.partition_key()
