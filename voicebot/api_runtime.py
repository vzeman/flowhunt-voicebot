from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter

from .api_models import DrainRequest
from .config import redacted_settings
from .deployment_topology import deployment_topology_payload, role_readiness_payload
from .drain import DrainState, rollout_contract
from .events import event_to_dict
from .health import readiness_report
from .runtime_storage import storage_drivers_payload
from .storage_contracts import storage_contracts_payload


@dataclass(frozen=True)
class RuntimeApiContext:
    events: Any
    registry: Any
    tracker: Any
    transcripts: Any
    asterisk: Any
    runtime_settings: Any
    workspace_access_policy: Any
    session_lease_store: Any
    scaling_workers: Any
    voicebot_session_store: Any
    provider_config_store: Any
    scaling_queue: Any
    subagent_coordinator: Any
    drain_state: DrainState

    def readiness_report(self) -> dict[str, Any]:
        subagent_store = (
            {"subagent_tasks": self.subagent_coordinator.store}
            if self.subagent_coordinator is not None
            else {}
        )
        return readiness_report(
            transcripts=self.transcripts,
            asterisk=self.asterisk,
            active_call_ids=self.registry.active_call_ids(),
            storage_components={
                "events": self.events,
                "agent_tasks": self.tracker,
                "call_states": self.registry.state_store,
                "session_leases": self.session_lease_store,
                "worker_registry": self.scaling_workers,
                "voicebot_sessions": self.voicebot_session_store,
                "provider_config": self.provider_config_store,
                "worker_queue": self.scaling_queue,
                **subagent_store,
            },
            drain_state=self.drain_state.snapshot(),
            settings=self.runtime_settings,
            workspace_policy=self.workspace_access_policy,
        )


def create_runtime_router(context: RuntimeApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "active_calls": context.registry.active_call_ids()}

    @router.get("/health/readiness")
    def readiness() -> dict[str, Any]:
        return context.readiness_report()

    @router.get("/health/readiness/roles")
    def role_readiness() -> dict[str, Any]:
        return role_readiness_payload(context.runtime_settings, context.readiness_report())

    @router.get("/health/liveness")
    def liveness() -> dict[str, Any]:
        return {"ok": True, "draining": context.drain_state.draining}

    @router.get("/config")
    def config() -> dict[str, Any]:
        return runtime_config_payload(context)

    @router.get("/deployment/topology")
    def deployment_topology() -> dict[str, Any]:
        return deployment_topology_payload(context.runtime_settings)

    @router.get("/operations/drain")
    def get_drain_state() -> dict[str, Any]:
        return {"drain": context.drain_state.snapshot(), "rollout": rollout_contract()}

    @router.post("/operations/drain/start")
    def start_drain(request: DrainRequest) -> dict[str, Any]:
        state = context.drain_state.start(request.reason)
        event = context.events.append("runtime", "runtime_draining_started", state)
        interrupted = []
        if request.interrupt_active_sessions:
            for snapshot in context.registry.snapshots():
                call_id = snapshot["call_id"]
                stopped = context.registry.stop(call_id)
                interrupted_event = context.events.append(
                    call_id,
                    "session_interrupted",
                    {
                        "reason": "runtime_draining",
                        "stopped": stopped,
                        "drain": state,
                        "workspace_id": (snapshot.get("route") or {}).get("workspace_id"),
                        "voicebot_id": (snapshot.get("route") or {}).get("voicebot_id"),
                    },
                )
                interrupted.append(event_to_dict(interrupted_event))
        context.events.append(
            "runtime",
            "metrics",
            {"name": "runtime_draining", "value": 1.0, "reason": state["reason"]},
        )
        return {"event_id": event.id, "drain": state, "interrupted": interrupted}

    @router.post("/operations/drain/stop")
    def stop_drain() -> dict[str, Any]:
        state = context.drain_state.stop()
        event = context.events.append("runtime", "runtime_draining_stopped", state)
        context.events.append("runtime", "metrics", {"name": "runtime_draining", "value": 0.0})
        return {"event_id": event.id, "drain": state}

    @router.get("/storage/contracts")
    def storage_contracts() -> dict[str, Any]:
        return storage_contracts_payload()

    @router.get("/storage/drivers")
    def storage_drivers() -> dict[str, Any]:
        return storage_drivers_payload(context.runtime_settings)

    return router


def runtime_config_payload(context: RuntimeApiContext) -> dict[str, Any]:
    return {"settings": redacted_settings(context.runtime_settings)}
