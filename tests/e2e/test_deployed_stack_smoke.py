from __future__ import annotations

import json
import os
import unittest
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest


pytestmark = pytest.mark.e2e


def _base_url() -> str:
    return os.environ.get("VOICEBOT_E2E_BASE_URL", "").rstrip("/")


def _workspace_id() -> str:
    return os.environ.get("VOICEBOT_E2E_WORKSPACE_ID", "workspace-1")


def _voicebot_id() -> str:
    return os.environ.get("VOICEBOT_E2E_VOICEBOT_ID", "voicebot-1")


def _timeout() -> float:
    return float(os.environ.get("VOICEBOT_E2E_TIMEOUT_SECONDS", "10"))


def _get_json(path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
    encoded_query = f"?{urlencode(query)}" if query else ""
    url = f"{_base_url()}{path}{encoded_query}"
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=_timeout()) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"GET {url} returned HTTP {exc.code}: {body}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"GET {url} returned non-JSON payload: {payload[:200]}") from exc
    if not isinstance(data, dict):
        raise AssertionError(f"GET {url} returned {type(data).__name__}, expected object")
    return data


@unittest.skipUnless(_base_url(), "set VOICEBOT_E2E_BASE_URL to run deployed E2E tests")
class DeployedStackSmokeTests(unittest.TestCase):
    def test_deployed_stack_exposes_runtime_readiness_contracts(self) -> None:
        health = _get_json("/health")
        readiness = _get_json("/health/readiness")
        event_catalog = _get_json("/events/catalog")
        storage_drivers = _get_json("/storage/drivers")

        self.assertIs(health["ok"], True)
        self.assertIsInstance(health["active_calls"], list)
        self.assertIn("components", readiness)
        self.assertIn("events", event_catalog)
        self.assertEqual(event_catalog["integrity_issues"], [])
        self.assertIn("drivers", storage_drivers)

    def test_deployed_stack_exposes_workspace_transport_catalog(self) -> None:
        catalog = _get_json(f"/workspaces/{_workspace_id()}/voicebots/{_voicebot_id()}/transports")

        self.assertEqual(catalog["workspace_id"], _workspace_id())
        self.assertEqual(catalog["voicebot_id"], _voicebot_id())
        self.assertIn("available_transports", catalog)
        self.assertIn("enabled_transports", catalog)

    def test_deployed_stack_event_stream_is_queryable(self) -> None:
        events = _get_json("/events", {"after": 0, "limit": 5})

        self.assertIn("events", events)
        self.assertIsInstance(events["events"], list)
        for event in events["events"]:
            self.assertIn("id", event)
            self.assertIn("call_id", event)
            self.assertIn("type", event)
            self.assertIn("timestamp", event)
            self.assertIn("data", event)
