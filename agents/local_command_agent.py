#!/usr/bin/env python3
"""
External AI-agent bridge for the voicebot API.

The voicebot service is intentionally agent-agnostic. This script polls pending
voicebot tasks, sends the event context to a local command over stdin, then posts
the command output back as the spoken answer.

Example:

    VOICEBOT_AGENT_COMMAND='codex exec -' python agents/local_command_agent.py

Use any command that accepts a prompt on stdin and writes the final answer to
stdout.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import difflib
import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll voicebot events and answer through a local AI command.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080", help="Voicebot API base URL.")
    parser.add_argument("--command", required=True, help="Local command to run for response generation.")
    parser.add_argument("--interval", type=float, default=0.5, help="Polling interval in seconds.")
    parser.add_argument("--command-timeout", type=float, default=30.0, help="AI command timeout in seconds.")
    return parser.parse_args()


def http_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=float(os.getenv("VOICEBOT_AGENT_HTTP_TIMEOUT", "90"))) as response:
        return json.loads(response.read().decode())


def build_prompt(tasks: list[dict], context: dict, tools: list[dict]) -> str:
    pending_lines = []
    for task in tasks:
        data = task.get("data", {})
        reason = data.get("reason", "user_transcript")
        if reason == "call_connected":
            label = "call event"
        elif reason in {"colleague_result", "colleague_progress"}:
            label = "colleague update"
        elif reason == "stale_transcript":
            label = "late transcript"
        else:
            label = "user said"
        pending_lines.append(
            f"- event_id={task['id']} call_id={task['call_id']} "
            f"reason={reason} {label}: {data.get('text', '')}"
        )
    output_format = {
        "say": "text to speak, if any",
        "tool_calls": [
            {"name": "hangup_call", "arguments": {"call_id": "...", "response_to_event_id": 123}},
            {"name": "transfer_call", "arguments": {"call_id": "...", "target": "123", "response_to_event_id": 123}},
            {"name": "send_dtmf", "arguments": {"call_id": "...", "digit": "1", "response_to_event_id": 123}},
            {
                "name": "invoke_flowhunt_flow",
                "arguments": {
                    "call_id": "...",
                    "message": "full task and context",
                    "response_to_event_id": 123,
                },
            },
            {
                "name": "create_flowhunt_project_issue",
                "arguments": {
                    "call_id": "...",
                    "title": "short title",
                    "description": "full task and context",
                    "response_to_event_id": 123,
                },
            },
        ],
    }

    return f"""You are an AI voicebot speaking with a customer on a phone call.
Your job is to help the caller, answer their questions, solve practical
problems, and use tools when a phone action is needed.

Do not repeat the caller's words back as the whole answer. Treat transcripts as
requests, not dictation. For normal questions, answer in one brief spoken
sentence, ideally under 18 words. Use two short sentences only when the caller
needs a result plus a next step. Do not mention implementation details, events,
queues, STT, TTS, Asterisk, or SIP. If there are multiple
unhandled user messages, answer them together in one coherent response.
You are the only voice the customer should hear. Colleague/project issue
updates are private working context, not scripts to read aloud. Turn them into
a fluent customer-facing answer: deduplicate repeated progress messages,
combine related updates, and only mention the useful result or the next clear
step. If a colleague result contains raw logs, JSON, issue statuses, or repeated
messages, extract the meaning and say it plainly. If the colleague task is still
running, give a short natural progress update only when it adds value; do not
repeat the same waiting message. Never call invoke_flowhunt_flow or
create_flowhunt_project_issue while handling a colleague update; use the
colleague update as the result or status of the already-running work.
If the caller asks to end the call, call the hangup_call tool. If the caller
asks to transfer the call, call transfer_call with the requested extension or
target. If the caller asks you to press or send a keypad digit, call send_dtmf
with one digit. Include response_to_event_id on every tool call.
Some pending messages may have reason=stale_transcript. That only means the STT
result arrived after newer caller audio started; it does not mean the text is
wrong. Use normal language understanding, in the caller's language, to decide
whether it is a still-actionable command or request. If it is just an obsolete
fragment already superseded by a newer pending message, merge it into context
or ignore it silently.
Only call hangup_call when the caller explicitly asks you to disconnect,
terminate, stop, or hang up the call. If a short transcript only says "bye" or
"goodbye", speak a brief farewell instead of using a tool.
If the caller asks something you can inspect on this computer, use your local
shell/tooling to find the answer before responding. If you cannot complete a
request, say what is missing and ask one short follow-up question.
For complex tasks in any caller language, website checks, account work,
research, comparisons, or anything that needs external tools, call
invoke_flowhunt_flow. It asks a FlowHunt colleague flow to work on the request
and returns or later emits the result. If the flow tool is not available, call
create_flowhunt_project_issue instead. When the tool or a later colleague update
returns information, use it to prepare a polished answer for the caller. Never
pretend you completed external work without a colleague result. Never answer by
saying only that you heard the request.
If a colleague result only says there were no incidents in a recent window, but
the caller asks for the last historical downtime, treat that as unresolved and
call invoke_flowhunt_flow again with an explicit archive/history request. Do
not answer from the limited recent-window result as if it were the final
historical answer.
When creating a colleague issue, base the title and description only on the
actual pending caller request and relevant conversation facts. Include the
caller request verbatim in the description. Do not create an issue from STT
prompt vocabulary, provider names, event metadata, or a vague topic list. If the
latest transcript is only a list or summary of possible topics, ask the caller
one short clarification question instead.

Conversation summary:
{context.get("summary") or "(none)"}

Recent events:
{json.dumps(context.get("events", []), ensure_ascii=False, indent=2)}

Pending user messages:
{chr(10).join(pending_lines)}

Available tools:
{json.dumps(tools, ensure_ascii=False, indent=2)}

Return either plain text to speak, or JSON in this form:
{json.dumps(output_format, ensure_ascii=False, indent=2)}
"""


def build_retry_prompt(original_prompt: str, bad_answer: str) -> str:
    return f"""{original_prompt}

Your previous answer was rejected because it repeated the caller instead of
helping them:
{bad_answer}

Produce a useful answer now. Do not use phrases like "I heard" or restate the
caller's request as the whole response.
"""


def build_tool_result_prompt(original_prompt: str, tool_results: list[dict]) -> str:
    return f"""{original_prompt}

Tool results from your previous step:
{json.dumps(tool_results, ensure_ascii=False, indent=2)[:12000]}

Now produce the final concise spoken answer for the caller. Prefer one brief
spoken sentence. Do not read tool results literally. Remove duplicate status
text, convert colleague/project updates into natural speech, and say only what
helps the caller understand the result or current progress. Do not call another
inspection tool. If the tool result is not enough, say what you could or could
not verify and ask one short follow-up question.
"""


def run_agent_command(command: str, prompt: str, timeout: float) -> str:
    output_file = tempfile.NamedTemporaryFile(prefix="flowhunt-agent-answer-", suffix=".txt", delete=False)
    output_file.close()
    effective_command = add_codex_output_file(command, output_file.name)
    try:
        completed = subprocess.run(
            effective_command,
            input=prompt,
            text=True,
            shell=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        with open(output_file.name, encoding="utf-8") as handle:
            final_answer = handle.read().strip()
        if completed.returncode != 0:
            error = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(error or f"agent command failed with {completed.returncode}")
        if final_answer:
            return final_answer
        stdout = completed.stdout.strip()
        if looks_like_codex_diagnostics(stdout):
            raise RuntimeError("agent command produced diagnostics but no final answer")
        return stdout
    finally:
        try:
            os.unlink(output_file.name)
        except OSError:
            pass


def add_codex_output_file(command: str, path: str) -> str:
    if "codex exec" not in command or "--output-last-message" in command:
        return command
    quoted_path = shlex.quote(path)
    if command.rstrip().endswith(" -"):
        return f"{command.rstrip()[:-2]} --output-last-message {quoted_path} -"
    return f"{command} --output-last-message {quoted_path}"


def looks_like_codex_diagnostics(output: str) -> bool:
    if not output:
        return False
    markers = (
        "stream error:",
        "unexpected status",
        "requires a newer version of codex",
        "mcp client",
        "error:",
    )
    lowered = output.lower()
    return any(marker in lowered for marker in markers)


def parse_agent_output(output: str) -> tuple[str, list[dict]]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return output, []
    if not isinstance(data, dict):
        return output, []
    tool_calls = data.get("tool_calls", [])
    if not isinstance(tool_calls, list):
        tool_calls = []
    say = data.get("say") or data.get("text") or ""
    return str(say).strip(), [call for call in tool_calls if isinstance(call, dict)]


def execute_tool_call(base_url: str, call: dict) -> dict:
    name = call.get("name")
    arguments = call.get("arguments") or {}
    if not name:
        raise RuntimeError("tool call missing name")
    if not isinstance(arguments, dict):
        raise RuntimeError(f"tool call {name} arguments must be an object")
    return http_json("POST", f"{base_url}/agent/tools/{name}", {"arguments": arguments})


def execute_tool_calls(base_url: str, calls: list[dict]) -> list[dict]:
    results = []
    for call in calls:
        name = call.get("name")
        try:
            result = execute_tool_call(base_url, call)
            results.append({"name": name, "ok": True, "result": result})
        except Exception as exc:
            results.append({"name": name, "ok": False, "error": str(exc)})
    return results


CALL_CONTROL_TOOL_NAMES = {"hangup_call", "transfer_call", "send_dtmf"}
BACKGROUND_WORK_TOOL_NAMES = {"delegate_to_subagent", "invoke_flowhunt_flow", "create_flowhunt_project_issue"}
COLLEAGUE_SPOKEN_MAX_CHARS = 190


def execute_conversational_tool_calls(base_url: str, calls: list[dict]) -> list[dict]:
    if not calls:
        return []
    say_calls = [call for call in calls if call.get("name") == "say"]
    control_calls = [call for call in calls if call.get("name") in CALL_CONTROL_TOOL_NAMES]
    background_calls = [call for call in calls if call.get("name") in BACKGROUND_WORK_TOOL_NAMES]
    other_calls = [
        call
        for call in calls
        if call.get("name") not in {"say", *CALL_CONTROL_TOOL_NAMES, *BACKGROUND_WORK_TOOL_NAMES}
    ]
    results = execute_speech_and_background_calls(base_url, say_calls, background_calls)
    results.extend(execute_tool_calls(base_url, other_calls))
    if say_calls and control_calls:
        wait_for_spoken_acknowledgement(base_url, say_calls[-1], control_ack_wait_seconds(say_calls[-1]))
    results.extend(execute_tool_calls(base_url, control_calls))
    return results


def execute_speech_and_background_calls(base_url: str, say_calls: list[dict], background_calls: list[dict]) -> list[dict]:
    if not say_calls:
        return execute_tool_calls(base_url, background_calls)
    if not background_calls:
        return execute_tool_calls(base_url, say_calls)
    with ThreadPoolExecutor(max_workers=2) as executor:
        speech_future = executor.submit(execute_tool_calls, base_url, say_calls)
        background_future = executor.submit(execute_tool_calls, base_url, background_calls)
        speech_results = speech_future.result()
        background_results = background_future.result()
    return [*speech_results, *background_results]


def wait_for_spoken_acknowledgement(base_url: str, say_call: dict, timeout_seconds: float = 1.2) -> None:
    if timeout_seconds <= 0:
        return
    call_id = (say_call.get("arguments") or {}).get("call_id")
    if not call_id:
        return
    deadline = time.monotonic() + timeout_seconds
    saw_playback = False
    while time.monotonic() < deadline:
        try:
            state = http_json("GET", f"{base_url}/calls/{call_id}")
        except Exception:
            return
        playback_active = bool(state.get("playback_active"))
        saw_playback = saw_playback or playback_active
        if saw_playback and not playback_active:
            return
        time.sleep(0.05)


def control_ack_wait_seconds(say_call: dict) -> float:
    args = say_call.get("arguments") if isinstance(say_call.get("arguments"), dict) else {}
    value = args.get("call_control_ack_wait_seconds", 1.2)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 1.2


READ_ONLY_TOOL_NAMES = {
    "list_transcripts",
    "list_transcript_summaries",
    "get_transcript_stats",
    "get_transcript",
    "get_events",
    "get_metrics",
    "get_active_calls",
    "get_call_state",
    "get_runtime_config",
    "get_agent_task_status",
    "get_agent_task_summary",
}


COLLEAGUE_TOOL_NAMES = {
    "invoke_flowhunt_flow",
    "create_flowhunt_project_issue",
}


VOICE_AGENT_TOOL_NAMES = {
    "say",
    "hangup_call",
    "transfer_call",
    "send_dtmf",
    "stop_playback",
    "invoke_flowhunt_flow",
    "create_flowhunt_project_issue",
}


def needs_spoken_followup(tool_calls: list[dict]) -> bool:
    return any(call.get("name") in READ_ONLY_TOOL_NAMES for call in tool_calls)


def action_acknowledgement(call: dict) -> dict | None:
    name = call.get("name")
    args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    call_id = args.get("call_id")
    if not call_id:
        return None
    response_to_event_id = args.get("response_to_event_id")
    if name == "hangup_call":
        text = "Goodbye."
        wait_seconds = 1.2
    elif name == "transfer_call":
        target = str(args.get("target") or "").strip()
        text = f"Transferring to {target} now." if target else "Transferring now."
        wait_seconds = 0.0
    elif name == "send_dtmf":
        digit = str(args.get("digit") or "").strip()
        text = f"Sending {digit} now." if digit else "Sending that now."
        wait_seconds = 0.0
    else:
        return None
    return {
        "name": "say",
        "arguments": {
            "call_id": call_id,
            "text": text,
            "response_to_event_id": response_to_event_id,
            "response_kind": "call_control_ack",
            "call_control_ack_wait_seconds": wait_seconds,
        },
    }


def ensure_action_acknowledgements(tool_calls: list[dict]) -> list[dict]:
    result: list[dict] = []
    for call in tool_calls:
        if call.get("name") in CALL_CONTROL_TOOL_NAMES:
            acknowledgement = action_acknowledgement(call)
            if acknowledgement and _has_prior_say(result):
                mark_prior_say_as_action_ack(result, acknowledgement)
            elif acknowledgement:
                result.append(acknowledgement)
        result.append(call)
    return result


def _has_prior_say(calls: list[dict]) -> bool:
    return any(call.get("name") == "say" for call in calls)


def mark_prior_say_as_action_ack(calls: list[dict], acknowledgement: dict) -> None:
    acknowledgement_args = acknowledgement.get("arguments") if isinstance(acknowledgement.get("arguments"), dict) else {}
    for call in reversed(calls):
        if call.get("name") != "say":
            continue
        args = call.setdefault("arguments", {})
        if isinstance(args, dict):
            args.setdefault("response_kind", "call_control_ack")
            args.setdefault("call_control_ack_wait_seconds", acknowledgement_args.get("call_control_ack_wait_seconds", 1.2))
        return


def answer_as_say_call(answer: str, latest: dict) -> dict | None:
    if not answer:
        return None
    return {
        "name": "say",
        "arguments": {
            "call_id": latest["call_id"],
            "text": answer,
            "response_to_event_id": latest["id"],
        },
    }


def is_colleague_update_task(task: dict) -> bool:
    return str(task.get("data", {}).get("reason") or "") in {"colleague_result", "colleague_progress"}


def remove_colleague_reentrant_tool_calls(tasks: list[dict], tool_calls: list[dict]) -> list[dict]:
    if not any(is_colleague_update_task(task) for task in tasks):
        return tool_calls
    return [call for call in tool_calls if call.get("name") not in COLLEAGUE_TOOL_NAMES]


def filter_voice_agent_tools(tools: list[dict]) -> list[dict]:
    return [tool for tool in tools if tool.get("name") in VOICE_AGENT_TOOL_NAMES]


def claim_tasks(base_url: str, tasks: list[dict], owner: str, ttl_seconds: float) -> list[dict]:
    event_ids = [int(task["id"]) for task in tasks]
    if not event_ids:
        return []
    response = http_json(
        "POST",
        f"{base_url}/agent/tasks/claim",
        {"event_ids": event_ids, "owner": owner, "ttl_seconds": ttl_seconds},
    )
    claimed_ids = set(response.get("claimed_event_ids", []))
    return [task for task in tasks if task["id"] in claimed_ids]


def release_tasks(base_url: str, tasks: list[dict], owner: str | None = None) -> dict:
    event_ids = [int(task["id"]) for task in tasks]
    if not event_ids:
        return {"released_event_ids": []}
    payload: dict = {"event_ids": event_ids}
    if owner:
        payload["owner"] = owner
    return http_json("POST", f"{base_url}/agent/tasks/release", payload)


def renew_tasks(base_url: str, tasks: list[dict], owner: str, ttl_seconds: float) -> dict:
    event_ids = [int(task["id"]) for task in tasks]
    if not event_ids:
        return {"renewed_event_ids": []}
    return http_json(
        "POST",
        f"{base_url}/agent/tasks/renew",
        {"event_ids": event_ids, "owner": owner, "ttl_seconds": ttl_seconds},
    )


class ClaimRenewer:
    def __init__(self, base_url: str, tasks: list[dict], owner: str, ttl_seconds: float) -> None:
        self.base_url = base_url
        self.tasks = tasks
        self.owner = owner
        self.ttl_seconds = ttl_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> ClaimRenewer:
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        interval = max(1.0, self.ttl_seconds / 2)
        while not self._stop.wait(interval):
            try:
                renew_tasks(self.base_url, self.tasks, self.owner, self.ttl_seconds)
            except (OSError, urllib.error.URLError, TimeoutError, RuntimeError):
                pass


def attach_response_event_id(tool_calls: list[dict], event_id: int) -> list[dict]:
    for call in tool_calls:
        arguments = call.setdefault("arguments", {})
        if isinstance(arguments, dict):
            arguments.setdefault("response_to_event_id", event_id)
    return tool_calls


def fast_tool_calls(task: dict) -> list[dict]:
    data = task.get("data", {})
    event_id = task["id"]
    call_id = task["call_id"]

    if data.get("reason") == "call_connected":
        return [{
            "name": "say",
            "arguments": {
                "call_id": call_id,
                "text": "Hello, this is the FlowHunt voicebot. How can I help you?",
                "response_to_event_id": event_id,
            },
        }]

    if is_colleague_update_task(task):
        answer = colleague_update_answer(task)
        if not answer:
            return []
        return [{
            "name": "say",
            "arguments": {
                "call_id": call_id,
                "text": answer,
                "response_to_event_id": event_id,
            },
        }]

    return []


def fast_tool_call(task: dict) -> dict | None:
    calls = fast_tool_calls(task)
    return calls[0] if calls else None


def colleague_update_answer(task: dict) -> str:
    data = task.get("data", {})
    reason = str(data.get("reason") or "")
    if reason == "colleague_progress":
        return "I am still checking that with a colleague."
    if reason != "colleague_result":
        return ""

    result = data.get("data") if isinstance(data.get("data"), dict) else {}
    candidate = ""
    if isinstance(result, dict):
        candidate = str(result.get("summary") or result.get("content") or "")
    if not candidate:
        candidate = str(data.get("text") or "")
        marker = "Result:"
        if marker in candidate:
            candidate = candidate.split(marker, 1)[1].strip()
    if not candidate:
        return ""
    spoken = customer_facing_colleague_text(candidate)
    if not spoken:
        return "I checked with a colleague, but I do not have a clear customer-facing result yet."
    return _speech_limit(f"I checked with a colleague. {spoken}", max_chars=COLLEAGUE_SPOKEN_MAX_CHARS)


def customer_facing_colleague_text(text: str) -> str:
    cleaned = strip_internal_colleague_text(text)
    customer_markers = (
        "customer-facing answer:",
        "customer facing answer:",
        "answer to the customer:",
        "final answer:",
        "answer:",
        "result:",
    )
    lowered = cleaned.lower()
    for marker in customer_markers:
        index = lowered.rfind(marker)
        if index >= 0:
            cleaned = cleaned[index + len(marker) :].strip()
            break
    cleaned = strip_internal_colleague_text(cleaned)
    return conversationalize_colleague_text(cleaned)


def strip_internal_colleague_text(text: str) -> str:
    cleaned = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    cleaned = re.sub(r"\{[^{}]*(?:task_id|status|workspace_id|flow_id|event_id)[^{}]*\}", " ", cleaned, flags=re.IGNORECASE)
    kept_lines = []
    for raw_line in cleaned.splitlines():
        line = raw_line.strip(" -*\t#")
        line = strip_leading_symbols(line)
        if not line:
            continue
        lowered = line.lower()
        if "you can share with your colleague" in lowered:
            line = re.sub(r"\byou can share with your colleague\b", "", line, flags=re.IGNORECASE).strip(" -:;,.")
            lowered = line.lower()
        if not line:
            continue
        if "taskstatus." in lowered:
            continue
        if lowered.startswith(
            (
                "internal",
                "debug",
                "log",
                "raw",
                "metadata",
                "status:",
                "task_id:",
                "workspace_id:",
                "flow_id:",
                "event_id:",
                "trace_id:",
                "tool result",
                "system:",
                "assistant:",
                "reference:",
            )
        ):
            continue
        if re.search(
            r"\b(hello and welcome|i'?m ai chatbot|i am ai chatbot|got any questions|feel free to ask)\b",
            lowered,
        ):
            continue
        if lowered.startswith(("if you tell me", "if you provide", "let me know if", "would you like me to")):
            continue
        kept_lines.append(line)
    return " ".join(kept_lines)


def conversationalize_colleague_text(text: str) -> str:
    pricing_summary = extract_plan_pricing_summary(text)
    if pricing_summary:
        return pricing_summary
    status_summary = extract_status_page_summary(text)
    if status_summary:
        return status_summary
    incident_summary = extract_incident_summary(text)
    if incident_summary:
        return incident_summary
    cleaned = re.sub(r"https?://\S+", "", text)
    cleaned = re.sub(r"[*_`#]", "", cleaned)
    cleaned = cleaned.replace("\u2705", "included").replace("\u274c", "not included")
    cleaned = re.sub(r"\b(?:completed|success|finished)\s*[:=-]\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:;,.")
    if not cleaned:
        return ""
    cleaned = re.sub(r"\bthe caller\b", "you", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bthe customer\b", "you", cleaned, flags=re.IGNORECASE)
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    useful = []
    for sentence in sentences:
        normalized = sentence.strip()
        if not normalized:
            continue
        if re.search(r"\b(task|ticket|issue|execution|metadata|log|json)\b", normalized, re.IGNORECASE):
            continue
        useful.append(normalized)
        if len(" ".join(useful)) >= 260 or len(useful) >= 2:
            break
    spoken = " ".join(useful) or cleaned
    if spoken and spoken[-1] not in ".!?":
        spoken = f"{spoken}."
    return _speech_limit(spoken, max_chars=COLLEAGUE_SPOKEN_MAX_CHARS - len("I checked with a colleague. "))


def strip_leading_symbols(text: str) -> str:
    return re.sub(r"^[^\w$]+", "", text, flags=re.UNICODE).strip()


def extract_status_page_summary(text: str) -> str:
    lowered = text.lower()
    if not ("status" in lowered and ("downtime" in lowered or "incident" in lowered or "operational" in lowered)):
        return ""
    normal = any(
        phrase in lowered
        for phrase in ("normal status", "operational", "no active downtime", "no ongoing incident")
    ) or re.search(r"\bno\s+(?:active\s+|visible\s+|ongoing\s+)?incidents?\b", lowered) is not None
    if not normal:
        return ""
    subject = "The status page"
    if "liveagent" in lowered:
        subject = "The LiveAgent status page"
    return f"{subject} currently shows normal operation, with no active downtime or visible incidents."


def extract_incident_summary(text: str) -> str:
    lowered = text.lower()
    if "incident" not in lowered and "downtime" not in lowered and "degradation" not in lowered:
        return ""

    date = extract_first_match(
        text,
        (
            r"\b([A-Z][a-z]+ \d{1,2}(?:st|nd|rd|th)?,? \d{4})\b",
            r"\b(\d{1,2} [A-Z][a-z]+ \d{4})\b",
        ),
    )
    if not date:
        return ""

    duration = extract_first_match(
        text,
        (
            r"\bDuration:\s*(?:Resolved in\s*)?([^.\n]+)",
            r"\bresolved in\s+([^.\n]+)",
        ),
    )
    issue = extract_first_match(
        text,
        (
            r"\bIssue:\s*([^.\n]+)",
            r"\bIt was an?\s+([^.\n]+)",
            r"\bthere was a\s+([^.\n]+)",
        ),
    )
    location = extract_first_match(
        text,
        (
            r"\b(?:Europe|EU)\s*\(([^)]+)\)\s*data center",
            r"\b(EU\s+[A-Z][A-Za-z]+)\s*data center",
            r"\b(Europe\s+[A-Z][A-Za-z]+)\s*data center",
        ),
    )

    issue_text = normalize_incident_issue(issue)
    parts = [f"Yes, {date} was a service degradation"]
    if location:
        location_text = location
        if not location_text.lower().startswith(("eu ", "europe ")):
            location_text = f"EU {location_text}"
        parts[0] += f" in {location_text}"
    if issue_text:
        parts.append(issue_text)
    if duration:
        parts.append(f"resolved in {normalize_incident_duration(duration)}")
    return f"{parts[0]}: {', '.join(parts[1:])}." if len(parts) > 1 else f"{parts[0]}."


def extract_first_match(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip(" -*:;,.")
    return ""


def normalize_incident_issue(issue: str) -> str:
    cleaned = re.sub(r"\s+", " ", issue).strip(" -*:;,.")
    if not cleaned:
        return ""
    cleaned = re.sub(r"\s+affecting\s+.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+in\s+the\s+.*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned


def normalize_incident_duration(duration: str) -> str:
    cleaned = re.sub(r"\s+", " ", duration).strip(" -*:;,.")
    cleaned = re.split(r"\b(?:Cause|Status|Issue|Most Recent Downtime)\s*:", cleaned, maxsplit=1, flags=re.IGNORECASE)[
        0
    ]
    return cleaned.strip(" -*:;,.")


def extract_plan_pricing_summary(text: str) -> str:
    if "call center" not in text.lower() or "$" not in text:
        return ""
    plans = []
    for plan in ("Small Business", "Medium Business", "Large Business", "Enterprise"):
        pattern = rf"{re.escape(plan)}.*?\$(\d+)[^$]+?\$(\d+).*?Call Center:\s*(.*?)(?:$|\n)"
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        monthly, annual, tail = match.groups()
        included = "not included" not in tail.lower()
        plans.append((plan, monthly, annual, included))
    if not plans:
        return ""

    included_plans = [plan for plan in plans if plan[3]]
    parts = []
    if included_plans:
        first = included_plans[0]
        parts.append(
            f"Call center functionality is included starting with {first[0]}, "
            f"which is ${first[1]} per agent per month, or ${first[2]} annually."
        )
    excluded = [plan for plan in plans if not plan[3]]
    if excluded:
        parts.append(f"{excluded[0][0]} does not include call center functionality.")
    return " ".join(parts)


def _speech_limit(text: str, max_chars: int = 900) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    truncated = cleaned[:max_chars].rsplit(".", 1)[0].strip()
    if len(truncated) < max_chars * 0.5:
        truncated = cleaned[:max_chars].rsplit(" ", 1)[0].strip()
    return f"{truncated}."


def is_echo_answer(answer: str, pending: list[dict]) -> bool:
    normalized_answer = _normalize(answer)
    if not normalized_answer:
        return False
    if normalized_answer.startswith(("i heard ", "you said ", "i heard you say ")):
        return True

    latest_text = _normalize(str(pending[-1].get("data", {}).get("text", "")))
    if not latest_text:
        return False
    if normalized_answer in {latest_text, f"i heard {latest_text}", f"you said {latest_text}"}:
        return True
    return difflib.SequenceMatcher(None, normalized_answer, latest_text).ratio() >= 0.88


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def main() -> None:
    args = parse_args()
    owner = f"local-command-agent:{os.getpid()}"
    seen: set[int] = set()
    claimed_pending: list[dict] = []

    while True:
        try:
            claimed_pending = []
            active_call_ids = set(http_json("GET", f"{args.base_url}/health").get("active_calls", []))
            response = http_json("GET", f"{args.base_url}/agent/tasks")
            pending = [
                task
                for task in response.get("pending", [])
                if task["id"] not in seen and task.get("call_id") in active_call_ids
            ]
            if not pending:
                time.sleep(args.interval)
                continue

            pending = claim_tasks(args.base_url, pending, owner, max(args.command_timeout * 2, 30.0))
            if not pending:
                time.sleep(args.interval)
                continue
            claimed_pending = pending

            ttl_seconds = max(args.command_timeout * 2, 30.0)
            with ClaimRenewer(args.base_url, pending, owner, ttl_seconds):
                latest = pending[-1]
                deterministic_calls = fast_tool_calls(latest)
                if deterministic_calls:
                    execute_conversational_tool_calls(args.base_url, deterministic_calls)
                    seen.add(latest["id"])
                    print(
                        f"executed {len(deterministic_calls)} deterministic tool(s) for event {latest['id']}",
                        flush=True,
                    )
                    claimed_pending = []
                    continue

                tools = filter_voice_agent_tools(http_json("GET", f"{args.base_url}/agent/tools").get("tools", []))
                prompt = build_prompt(pending, response.get("context", {}), tools)
                raw_answer = run_agent_command(args.command, prompt, args.command_timeout)
                answer, tool_calls = parse_agent_output(raw_answer)
                if answer and is_echo_answer(answer, pending):
                    raw_answer = run_agent_command(args.command, build_retry_prompt(prompt, answer), args.command_timeout)
                    answer, tool_calls = parse_agent_output(raw_answer)
                    if answer and is_echo_answer(answer, pending):
                        raise RuntimeError(f"agent returned echo response twice: {answer}")
                tool_calls = attach_response_event_id(tool_calls, latest["id"])
                tool_calls = remove_colleague_reentrant_tool_calls(pending, tool_calls)
                calls_to_execute = []
                say_call = answer_as_say_call(answer, latest)
                if say_call:
                    calls_to_execute.append(say_call)
                calls_to_execute.extend(tool_calls)
                execute_conversational_tool_calls(args.base_url, ensure_action_acknowledgements(calls_to_execute))
            for task in pending:
                seen.add(task["id"])
            claimed_pending = []
            print(f"answered {len(pending)} pending event(s) for call {latest['call_id']}", flush=True)
        except (OSError, urllib.error.URLError, TimeoutError, RuntimeError, subprocess.SubprocessError) as exc:
            if claimed_pending:
                try:
                    release_tasks(args.base_url, claimed_pending, owner)
                except (OSError, urllib.error.URLError, TimeoutError, RuntimeError):
                    pass
                claimed_pending = []
            print(f"agent error: {exc}", flush=True)
            time.sleep(max(args.interval, 2.0))


if __name__ == "__main__":
    main()
