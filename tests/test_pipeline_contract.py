from __future__ import annotations

import tempfile
import unittest

from fastapi.testclient import TestClient

from voicebot.agent_tasks import AgentTaskTracker
from voicebot.api import WebSocketHub, create_app
from voicebot.calls import CallRegistry
from voicebot.events import EventStore
from voicebot.pipeline_contract import (
    PIPELINE_CONTRACT_VERSION,
    REQUIRED_PIPELINE_STAGES,
    TRANSPORT_PIPELINE_MAPPING,
    pipeline_contract_issues,
    pipeline_contract_payload,
)
from voicebot.transcripts import TranscriptStore


class PipelineContractTests(unittest.TestCase):
    def test_pipeline_contract_is_valid(self) -> None:
        self.assertEqual(pipeline_contract_issues(), [])

    def test_pipeline_contract_contains_required_stages_in_order(self) -> None:
        payload = pipeline_contract_payload()

        self.assertEqual(payload["version"], PIPELINE_CONTRACT_VERSION)
        self.assertEqual([stage["name"] for stage in payload["stages"]], list(REQUIRED_PIPELINE_STAGES))

    def test_sip_and_webrtc_map_to_same_conceptual_pipeline(self) -> None:
        self.assertEqual(
            TRANSPORT_PIPELINE_MAPPING["asterisk_audiosocket"],
            TRANSPORT_PIPELINE_MAPPING["webrtc"],
        )
        self.assertEqual(TRANSPORT_PIPELINE_MAPPING["webrtc"], REQUIRED_PIPELINE_STAGES)

    def test_replaceable_provider_stages_declare_provider_family(self) -> None:
        stages = {stage["name"]: stage for stage in pipeline_contract_payload()["stages"]}

        self.assertEqual(stages["stt"]["provider_config_family"], "stt")
        self.assertEqual(stages["communication_agent"]["provider_config_family"], "agent")
        self.assertEqual(stages["tts"]["provider_config_family"], "tts")
        self.assertEqual(stages["subagent_delegation"]["provider_config_family"], "subagent")

    def test_pipeline_contract_endpoint_returns_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                EventStore(max_context_events=20),
                CallRegistry(),
                AgentTaskTracker(),
                WebSocketHub(),
                TranscriptStore(directory),
                None,
            )
            client = TestClient(app)

            response = client.get("/pipeline/contract")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), pipeline_contract_payload())
