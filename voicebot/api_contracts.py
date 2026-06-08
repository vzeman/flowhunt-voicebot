from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter

from .pipeline_contract import pipeline_contract_payload
from .realtime_quality import realtime_audio_profile, realtime_audio_profile_issues


@dataclass(frozen=True)
class ContractsApiContext:
    runtime_settings: Any


def create_contracts_router(context: ContractsApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/pipeline/contract")
    def pipeline_contract() -> dict[str, Any]:
        return pipeline_contract_payload()

    @router.get("/realtime/audio-profile")
    def get_realtime_audio_profile() -> dict[str, Any]:
        profile = realtime_audio_profile(context.runtime_settings)
        return {"profile": profile, "issues": realtime_audio_profile_issues(profile)}

    return router
