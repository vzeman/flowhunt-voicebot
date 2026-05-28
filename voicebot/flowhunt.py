from __future__ import annotations

from dataclasses import dataclass
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

try:
    import flowhunt
    from flowhunt.api.flows_api import FlowsApi
    from flowhunt.api.flow_assistant_v3_api import FlowAssistantV3Api
    from flowhunt.models.flow_invoke_request import FlowInvokeRequest
    from flowhunt.models.flow_assistant_invoke_request import FlowAssistantInvokeRequest
    from flowhunt.models.flow_assistant_session_create_request import FlowAssistantSessionCreateRequest
except ModuleNotFoundError:
    flowhunt = None
    FlowsApi = None
    FlowAssistantV3Api = None
    FlowInvokeRequest = None
    FlowAssistantInvokeRequest = None
    FlowAssistantSessionCreateRequest = None


TERMINAL_ISSUE_STATES = {
    "done",
    "completed",
    "complete",
    "finished",
    "resolved",
    "closed",
    "success",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "human_input_needed",
}

FLOW_TASK_PENDING_STATES = {
    "pending",
    "queued",
    "running",
    "processing",
    "started",
    "in_progress",
    "taskstatus.pending",
    "flowinvokeresponsestatus.pending",
}

FLOW_TASK_TERMINAL_STATES = {
    "done",
    "completed",
    "complete",
    "success",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "taskstatus.done",
    "taskstatus.completed",
    "taskstatus.success",
    "taskstatus.failed",
    "taskstatus.error",
    "taskstatus.cancelled",
    "taskstatus.canceled",
}


@dataclass(frozen=True)
class FlowHuntResult:
    ok: bool
    message: str
    data: dict[str, Any]


class FlowHuntClient:
    def __init__(
        self,
        api_key: str,
        workspace_id: str,
        base_url: str = "https://api.flowhunt.io",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.workspace_id = workspace_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def create_project_issue(
        self,
        project_id: str,
        title: str,
        description: str,
        metadata: dict[str, Any] | None = None,
    ) -> FlowHuntResult:
        if not self.api_key:
            return FlowHuntResult(False, "FlowHunt API key is not configured", {})
        if not self.workspace_id:
            return FlowHuntResult(False, "FlowHunt workspace ID is not configured", {})
        if not project_id:
            return FlowHuntResult(False, "FlowHunt project ID is required", {})

        payload = {
            "title": title,
            "description": description,
            "content": description,
            "human_input": description,
            "metadata": metadata or {},
        }
        return self._request("POST", f"/v2/projects/{project_id}/issues/create", payload)

    def get_project_issue(self, project_id: str, issue_id: str) -> FlowHuntResult:
        return self._request("GET", f"/v2/projects/{project_id}/issues/{issue_id}")

    def create_issue_and_wait(
        self,
        project_id: str,
        title: str,
        description: str,
        metadata: dict[str, Any] | None = None,
        wait_seconds: float = 45.0,
        poll_interval_seconds: float = 2.0,
    ) -> FlowHuntResult:
        created = self.create_project_issue(project_id, title, description, metadata)
        if not created.ok:
            return created

        issue_id = extract_issue_id(created.data.get("response")) or extract_issue_id(created.data)
        if not issue_id:
            return FlowHuntResult(True, created.message or "FlowHunt project issue was created.", created.data)

        deadline = time.monotonic() + max(0.0, wait_seconds)
        latest = created
        while time.monotonic() < deadline:
            time.sleep(max(0.2, poll_interval_seconds))
            latest = self.get_project_issue(project_id, issue_id)
            if not latest.ok:
                return latest
            response = latest.data.get("response")
            state = extract_issue_state(response).lower()
            if is_terminal_issue_state(state):
                break
            if extract_issue_result(response):
                break

        response = latest.data.get("response")
        result = extract_issue_result(response)
        state = extract_issue_state(response)
        if result:
            return FlowHuntResult(True, result, latest.data)
        if state:
            return FlowHuntResult(True, f"FlowHunt project issue is {state}.", latest.data)
        return FlowHuntResult(True, "FlowHunt project issue was created and is still being processed.", latest.data)

    def invoke_flow_and_wait(
        self,
        flow_id: str,
        message: str,
        wait_seconds: float = 45.0,
        poll_interval_seconds: float = 2.0,
    ) -> FlowHuntResult:
        if not self.api_key:
            return FlowHuntResult(False, "FlowHunt API key is not configured", {})
        if not self.workspace_id:
            return FlowHuntResult(False, "FlowHunt workspace ID is not configured", {})
        if not flow_id:
            return FlowHuntResult(False, "FlowHunt flow ID is required", {})
        if not message.strip():
            return FlowHuntResult(False, "FlowHunt flow message is required", {})

        sdk_result = self._invoke_flow_with_v2_sdk(flow_id, message, wait_seconds, poll_interval_seconds)
        if sdk_result is not None:
            return sdk_result
        return self._invoke_flow_with_v2_api(flow_id, message, wait_seconds, poll_interval_seconds)

    def _invoke_flow_with_v2_sdk(
        self,
        flow_id: str,
        message: str,
        wait_seconds: float,
        poll_interval_seconds: float,
    ) -> FlowHuntResult | None:
        if flowhunt is None or FlowsApi is None or FlowInvokeRequest is None:
            return None
        try:
            configuration = flowhunt.Configuration(
                host=self.base_url,
                api_key={"APIKeyHeader": self.api_key},
            )
            api = FlowsApi(flowhunt.ApiClient(configuration))
            invoked = api.invoke_flow(
                flow_id,
                self.workspace_id,
                FlowInvokeRequest(human_input=message, variables={}),
                _request_timeout=self.timeout,
            )
            task = object_to_data(invoked)
            return self._wait_for_flow_task(flow_id, task, wait_seconds, poll_interval_seconds, message)
        except Exception as exc:
            message_text = str(exc)
            if "401" in message_text or "403" in message_text or "Unauthorized" in message_text:
                return FlowHuntResult(False, message_text, {})
            return None

    def _invoke_flow_with_v2_api(
        self,
        flow_id: str,
        message: str,
        wait_seconds: float,
        poll_interval_seconds: float,
    ) -> FlowHuntResult:
        invoked = self._request(
            "POST",
            f"/v2/flows/{urllib.parse.quote(flow_id)}/invoke",
            {"human_input": message, "variables": {}},
        )
        if not invoked.ok:
            return invoked
        task = invoked.data.get("response")
        return self._wait_for_flow_task(flow_id, task, wait_seconds, poll_interval_seconds, message)

    def _wait_for_flow_task(
        self,
        flow_id: str,
        task: Any,
        wait_seconds: float,
        poll_interval_seconds: float,
        message: str = "",
    ) -> FlowHuntResult:
        task_id = extract_flow_task_id(task)
        result = extract_flow_task_result(task)
        if result:
            return FlowHuntResult(True, result, {"flow_id": flow_id, "task_id": task_id, "message": message, "response": task})
        if is_flow_task_terminal(task):
            return FlowHuntResult(False, extract_flow_task_error(task) or "FlowHunt flow finished without a result.", {"flow_id": flow_id, "task_id": task_id, "message": message, "response": task})
        if not task_id:
            return FlowHuntResult(True, "FlowHunt flow was invoked.", {"flow_id": flow_id, "message": message, "response": task})

        deadline = time.monotonic() + max(0.0, wait_seconds)
        latest = FlowHuntResult(True, "FlowHunt flow was invoked.", {"response": task})
        while time.monotonic() < deadline:
            time.sleep(max(0.2, poll_interval_seconds))
            latest = self.get_flow_task(flow_id, task_id)
            if not latest.ok:
                return latest
            task = latest.data.get("response")
            result = extract_flow_task_result(task)
            if result:
                return FlowHuntResult(True, result, {"flow_id": flow_id, "task_id": task_id, "message": message, "response": task})
            if is_flow_task_terminal(task):
                return FlowHuntResult(False, extract_flow_task_error(task) or "FlowHunt flow finished without a result.", {"flow_id": flow_id, "task_id": task_id, "message": message, "response": task})
        return FlowHuntResult(
            True,
            "The FlowHunt flow is still processing the request.",
            {"pending": True, "flow_id": flow_id, "task_id": task_id, "message": message, "response": latest.data.get("response")},
        )

    def get_flow_task(self, flow_id: str, task_id: str) -> FlowHuntResult:
        sdk_result = self._get_flow_task_with_v2_sdk(flow_id, task_id)
        if sdk_result is not None:
            return sdk_result
        return self._request("GET", f"/v2/flows/{urllib.parse.quote(flow_id)}/{urllib.parse.quote(task_id)}")

    def _get_flow_task_with_v2_sdk(self, flow_id: str, task_id: str) -> FlowHuntResult | None:
        if flowhunt is None or FlowsApi is None:
            return None
        try:
            configuration = flowhunt.Configuration(
                host=self.base_url,
                api_key={"APIKeyHeader": self.api_key},
            )
            api = FlowsApi(flowhunt.ApiClient(configuration))
            task = api.get_invoked_flow_results(
                flow_id,
                task_id,
                self.workspace_id,
                _request_timeout=self.timeout,
            )
            data = object_to_data(task)
            return FlowHuntResult(
                True,
                extract_flow_task_result(data) or extract_flow_task_error(data) or str(data.get("status") or ""),
                {"response": data},
            )
        except Exception as exc:
            message = str(exc)
            if "401" in message or "403" in message or "Unauthorized" in message:
                return FlowHuntResult(False, message, {})
            return None

    def poll_flow_events(self, session_id: str, from_timestamp: str = "0") -> FlowHuntResult:
        if not session_id:
            return FlowHuntResult(False, "FlowHunt flow session ID is required", {})
        if FlowAssistantV3Api is not None and flowhunt is not None:
            try:
                configuration = flowhunt.Configuration(
                    host=self.base_url,
                    access_token=self.api_key,
                    api_key={"APIKeyHeader": self.api_key},
                )
                api = FlowAssistantV3Api(flowhunt.ApiClient(configuration))
                events = api.poll_v3_flow_assistant_response(session_id, from_timestamp, _request_timeout=self.timeout)
                data_events = [object_to_data(event) for event in (events or [])]
                result = extract_flow_result_from_events(data_events)
                return FlowHuntResult(
                    True,
                    result or "The FlowHunt flow is still processing the request.",
                    {"session_id": session_id, "events": data_events, "pending": not bool(result)},
                )
            except Exception:
                pass
        polled = self._request(
            "POST",
            f"/v3/flow-assistants/{urllib.parse.quote(session_id)}/invocation_response/{urllib.parse.quote(from_timestamp)}",
        )
        if not polled.ok:
            return polled
        events = normalize_event_list(polled.data.get("response"))
        result = extract_flow_result_from_events(events)
        return FlowHuntResult(
            True,
            result or "The FlowHunt flow is still processing the request.",
            {"session_id": session_id, "events": events, "pending": not bool(result)},
        )

    def _invoke_flow_with_sdk(
        self,
        flow_id: str,
        message: str,
        wait_seconds: float,
        poll_interval_seconds: float,
    ) -> FlowHuntResult | None:
        if FlowAssistantV3Api is None or flowhunt is None:
            return None
        try:
            configuration = flowhunt.Configuration(
                host=self.base_url,
                access_token=self.api_key,
                api_key={"APIKeyHeader": self.api_key},
            )
            api_client = flowhunt.ApiClient(configuration)
            api = FlowAssistantV3Api(api_client)
            session = api.create_v3_flow_assistant_session(
                self.workspace_id,
                FlowAssistantSessionCreateRequest(context_flow_id=flow_id, start_with_welcome_message=False),
                _request_timeout=self.timeout,
            )
            session_id = str(getattr(session, "session_id", "") or "")
            if not session_id:
                return FlowHuntResult(False, "FlowHunt flow session did not return a session ID", {"response": object_to_data(session)})
            invoked = api.invoke_v3_flow_assistant_response(
                session_id,
                FlowAssistantInvokeRequest(message=message),
                _request_timeout=self.timeout,
            )
            cached_events = getattr(invoked, "cached_events", None) or []
            result = extract_flow_result_from_events([object_to_data(event) for event in cached_events])
            if result:
                return FlowHuntResult(
                    True,
                    result,
                    {"session_id": session_id, "flow_id": flow_id, "response": object_to_data(invoked), "events": [object_to_data(event) for event in cached_events]},
                )
            from_timestamp = str(getattr(invoked, "created_at", "") or "0")
            deadline = time.monotonic() + max(0.0, wait_seconds)
            events: list[dict[str, Any]] = [object_to_data(event) for event in cached_events]
            latest_timestamp = from_timestamp
            while time.monotonic() < deadline:
                time.sleep(max(0.2, poll_interval_seconds))
                polled = api.poll_v3_flow_assistant_response(session_id, latest_timestamp, _request_timeout=self.timeout)
                new_events = [object_to_data(event) for event in (polled or [])]
                events.extend(new_events)
                latest_timestamp = newest_flow_event_timestamp(new_events, latest_timestamp)
                result = extract_flow_result_from_events(events)
                if result:
                    return FlowHuntResult(
                        True,
                        result,
                        {"session_id": session_id, "flow_id": flow_id, "events": events},
                    )
            return FlowHuntResult(
                True,
                "The FlowHunt flow is still processing the request.",
                {"pending": True, "session_id": session_id, "flow_id": flow_id, "events": events},
            )
        except Exception as exc:
            return FlowHuntResult(False, str(exc), {})

    def _invoke_flow_with_rest(
        self,
        flow_id: str,
        message: str,
        wait_seconds: float,
        poll_interval_seconds: float,
    ) -> FlowHuntResult:
        created = self._request(
            "POST",
            "/v3/flow-assistants/create",
            {"context_flow_id": flow_id, "start_with_welcome_message": False},
            bearer=True,
        )
        if not created.ok:
            return created
        session_id = extract_session_id(created.data.get("response")) or extract_session_id(created.data)
        if not session_id:
            return FlowHuntResult(False, "FlowHunt flow session did not return a session ID", created.data)

        invoked = self._request(
            "POST",
            f"/v3/flow-assistants/{urllib.parse.quote(session_id)}/invoke",
            {"message": message},
        )
        if not invoked.ok:
            return invoked
        response = invoked.data.get("response")
        events = list(extract_cached_events(response))
        result = extract_flow_result_from_events(events)
        if result:
            return FlowHuntResult(True, result, {"session_id": session_id, "flow_id": flow_id, "events": events, "response": response})

        from_timestamp = str(nested_get(response, "created_at") or "0")
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while time.monotonic() < deadline:
            time.sleep(max(0.2, poll_interval_seconds))
            polled = self._request(
                "POST",
                f"/v3/flow-assistants/{urllib.parse.quote(session_id)}/invocation_response/{urllib.parse.quote(from_timestamp)}",
            )
            if not polled.ok:
                return polled
            new_events = normalize_event_list(polled.data.get("response"))
            events.extend(new_events)
            from_timestamp = newest_flow_event_timestamp(new_events, from_timestamp)
            result = extract_flow_result_from_events(events)
            if result:
                return FlowHuntResult(True, result, {"session_id": session_id, "flow_id": flow_id, "events": events})

        return FlowHuntResult(
            True,
            "The FlowHunt flow is still processing the request.",
            {"pending": True, "session_id": session_id, "flow_id": flow_id, "events": events},
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        bearer: bool = False,
    ) -> FlowHuntResult:
        if not self.api_key:
            return FlowHuntResult(False, "FlowHunt API key is not configured", {})
        query = urllib.parse.urlencode({"workspace_id": self.workspace_id})
        separator = "&" if "?" in path else "?"
        url = f"{self.base_url}{path}{separator}{query}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Api-Key": self.api_key,
                **({"Authorization": f"Bearer {self.api_key}"} if bearer else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            data = parse_json_object(raw)
            return FlowHuntResult(
                False,
                extract_message(data) or raw[:1000] or str(exc),
                {"status": exc.code, "response": data or raw[:4000]},
            )
        except Exception as exc:
            return FlowHuntResult(False, str(exc), {})

        data = parse_json_object(raw)
        return FlowHuntResult(
            200 <= status < 300,
            extract_message(data) or raw[:1000],
            {"status": status, "response": data or raw[:4000]},
        )


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {"value": data}


def extract_issue_id(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("issue_id", "id", "project_issue_id"):
            value = data.get(key)
            if value:
                return str(value)
        for value in data.values():
            issue_id = extract_issue_id(value)
            if issue_id:
                return issue_id
    return ""


def extract_session_id(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("session_id", "id"):
            value = data.get(key)
            if value:
                return str(value)
        for value in data.values():
            session_id = extract_session_id(value)
            if session_id:
                return session_id
    return ""


def extract_flow_task_id(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("task_id", "id"):
            value = data.get(key)
            if value:
                return str(value)
    return ""


def extract_flow_task_result(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    result = data.get("result")
    if isinstance(result, str):
        parsed = parse_json_object(result)
        if parsed:
            return extract_flow_result_payload(parsed)
        return result.strip()
    return extract_flow_result_payload(result)


def extract_flow_result_payload(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("ai_answer", "answer", "result", "response", "output", "message"):
            text = stringify_value(data.get(key))
            if text:
                return text
        outputs = data.get("outputs")
        if isinstance(outputs, list):
            text = stringify_value(outputs)
            if text:
                return text
        if {"outputs", "ai_answer", "status"} & set(data):
            return ""
    return stringify_value(data)


def is_flow_task_terminal(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    status = normalize_flow_task_status(data.get("status"))
    if status in FLOW_TASK_PENDING_STATES:
        return False
    if status in FLOW_TASK_TERMINAL_STATES:
        return True
    if status:
        return False
    result = data.get("result")
    parsed = parse_json_object(result) if isinstance(result, str) else result
    if isinstance(parsed, dict):
        inner_status = normalize_flow_task_status(parsed.get("status"))
        return inner_status in FLOW_TASK_TERMINAL_STATES
    return False


def normalize_flow_task_status(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    if "." in text:
        return text.split(".")[-1]
    return text


def extract_flow_task_error(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("error_message", "message", "detail"):
        text = stringify_value(data.get(key))
        if text:
            return text
    result = data.get("result")
    parsed = parse_json_object(result) if isinstance(result, str) else result
    if isinstance(parsed, dict):
        for key in ("error_message", "error", "error_type"):
            text = stringify_value(parsed.get(key))
            if text:
                return text
    return ""


def extract_issue_state(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("status", "state", "issue_status", "run_status"):
            value = data.get(key)
            if value:
                return str(value)
    return ""


def is_terminal_issue_state(state: str) -> bool:
    return state.strip().lower() in TERMINAL_ISSUE_STATES


def extract_issue_result(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("result", "answer", "final_answer", "output", "response", "summary", "resolution"):
            text = stringify_value(data.get(key))
            if text:
                return text
    return ""


def extract_issue_updates(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("updates", "messages", "comments", "events", "logs"):
            text = stringify_issue_updates(data.get(key))
            if text:
                return text
    return ""


def extract_message(data: dict[str, Any]) -> str:
    for key in ("message", "detail", "error", "answer", "result"):
        text = stringify_value(data.get(key))
        if text:
            return text
    return ""


def extract_flow_result_from_events(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        metadata = event.get("metadata") or {}
        event_type = str(event.get("event_type") or "").lower()
        action_type = str(event.get("action_type") or "").lower()
        if event_type not in {"ai", "flow_assistant_action", ""}:
            continue
        for candidate in (
            nested_get(metadata, "message"),
            nested_get(metadata, "task_response"),
            nested_get(metadata, "result"),
            nested_get(metadata, "answer"),
            nested_get(event, "message"),
            nested_get(event, "task_response"),
        ):
            text = stringify_value(candidate)
            if text and not looks_like_flow_progress(text):
                return text
        if action_type == "message":
            text = stringify_value(metadata)
            if text and not looks_like_flow_progress(text):
                return text
    return ""


def looks_like_flow_progress(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in {"", "loading", "thinking", "processing"} or normalized.startswith("loading")


def extract_cached_events(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        return normalize_event_list(data.get("cached_events") or data.get("events") or [])
    return []


def normalize_event_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [object_to_data(item) for item in data if isinstance(object_to_data(item), dict)]
    if isinstance(data, dict):
        for key in ("events", "items", "value"):
            events = normalize_event_list(data.get(key))
            if events:
                return events
    return []


def newest_flow_event_timestamp(events: list[dict[str, Any]], fallback: str) -> str:
    timestamps = [
        str(event.get("created_at_timestamp") or event.get("created_at") or "")
        for event in events
        if isinstance(event, dict) and (event.get("created_at_timestamp") or event.get("created_at"))
    ]
    return max(timestamps) if timestamps else fallback


def nested_get(data: Any, key: str) -> Any:
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for value in data.values():
            found = nested_get(value, key)
            if found is not None:
                return found
    return None


def object_to_data(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def stringify_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [stringify_value(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("message", "text", "content", "answer", "output", "value", "result"):
            text = stringify_value(value.get(key))
            if text:
                return text
        return json.dumps(value, ensure_ascii=False)[:2000]
    return str(value).strip()


def stringify_issue_updates(value: Any) -> str:
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                text = stringify_value(item.get("result") or item.get("answer") or item.get("content") or item.get("text"))
            else:
                text = stringify_value(item)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    return stringify_value(value)
