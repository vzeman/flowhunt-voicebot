from __future__ import annotations

import json
import os
import unittest
import uuid
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


def _post_json(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{_base_url()}{path}"
    body = json.dumps(payload or {}).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=_timeout()) as response:
            raw_payload = response.read().decode("utf-8")
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"POST {url} returned HTTP {exc.code}: {error_body}") from exc

    try:
        data = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"POST {url} returned non-JSON payload: {raw_payload[:200]}") from exc
    if not isinstance(data, dict):
        raise AssertionError(f"POST {url} returned {type(data).__name__}, expected object")
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

    def test_deployed_stack_worker_queue_lifecycle_crosses_http_boundary(self) -> None:
        item_id = f"e2e-{uuid.uuid4().hex}"
        queue = "voicebot.e2e"
        owner = f"e2e-worker-{uuid.uuid4().hex[:8]}"

        enqueued = _post_json(
            "/scaling/queue/enqueue",
            {
                "item_id": item_id,
                "kind": "agent_turn",
                "routing": {
                    "workspace_id": _workspace_id(),
                    "voicebot_id": _voicebot_id(),
                    "session_id": item_id,
                },
                "queue": queue,
                "payload": {"event_id": 1, "source": "deployed-e2e"},
                "idempotency_key": item_id,
                "max_attempts": 2,
            },
        )
        claimed = _post_json("/scaling/queue/claim", {"queue": queue, "owner": owner, "limit": 1, "ttl_seconds": 30})
        renewed = _post_json("/scaling/queue/renew", {"item_id": item_id, "owner": owner, "ttl_seconds": 30})
        released = _post_json("/scaling/queue/release", {"item_id": item_id, "owner": owner, "error": "e2e retry"})
        reclaimed = _post_json("/scaling/queue/claim", {"queue": queue, "owner": owner, "limit": 1, "ttl_seconds": 30})
        acked = _post_json("/scaling/queue/ack", {"item_id": item_id, "owner": owner})

        self.assertEqual(enqueued["item"]["item_id"], item_id)
        self.assertEqual([item["item_id"] for item in claimed["items"]], [item_id])
        self.assertTrue(renewed["renewed"])
        self.assertTrue(released["released"])
        self.assertEqual([item["item_id"] for item in reclaimed["items"]], [item_id])
        self.assertTrue(acked["acked"])

    def test_deployed_stack_session_lease_events_are_observable(self) -> None:
        session_id = f"e2e-session-{uuid.uuid4().hex}"
        call_id = f"e2e-call-{uuid.uuid4().hex}"
        request = {
            "workspace_id": _workspace_id(),
            "voicebot_id": _voicebot_id(),
            "session_id": session_id,
            "owner": "deployed-e2e",
            "ttl_seconds": 30,
            "call_id": call_id,
            "transport": "webrtc",
            "metadata": {"source": "deployed-e2e"},
        }

        acquired = _post_json("/scaling/session-leases/acquire", request)
        renewed = _post_json("/scaling/session-leases/renew", request)
        listed = _get_json("/scaling/session-leases", {"workspace_id": _workspace_id(), "voicebot_id": _voicebot_id()})
        released = _post_json(
            "/scaling/session-leases/release",
            {
                "workspace_id": _workspace_id(),
                "voicebot_id": _voicebot_id(),
                "session_id": session_id,
                "owner": "deployed-e2e",
            },
        )
        timeline = _get_json("/events", {"call_id": call_id, "after": 0, "limit": 20})

        self.assertTrue(acquired["acquired"])
        self.assertTrue(renewed["renewed"])
        self.assertIn(session_id, {lease["session_id"] for lease in listed["leases"]})
        self.assertTrue(released["released"])
        self.assertIn(
            "session_lease_acquired",
            {event["type"] for event in timeline["events"]},
        )
        self.assertIn(
            "session_lease_released",
            {event["type"] for event in timeline["events"]},
        )
