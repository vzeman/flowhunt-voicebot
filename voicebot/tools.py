from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ToolName = Literal[
    "say",
    "hangup_call",
    "transfer_call",
    "send_dtmf",
    "stop_playback",
    "delegate_to_subagent",
    "invoke_flowhunt_flow",
    "create_flowhunt_project_issue",
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
]


@dataclass(frozen=True)
class ToolArgument:
    name: str
    description: str
    required: bool = True
    schema: dict[str, Any] = field(default_factory=lambda: {"type": "string"})


@dataclass(frozen=True)
class ToolDefinition:
    name: ToolName
    description: str
    arguments: tuple[ToolArgument, ...]

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "arguments": {argument.name: argument.description for argument in self.arguments},
        }

    def to_json_schema(self) -> dict[str, Any]:
        properties = {argument.name: argument.schema for argument in self.arguments}
        required = [argument.name for argument in self.arguments if argument.required]
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }


@dataclass(frozen=True)
class ToolCall:
    name: ToolName
    arguments: dict[str, Any] = field(default_factory=dict)


TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        "say",
        "Speak text into an active call.",
        (
            ToolArgument("call_id", "Active call ID."),
            ToolArgument("text", "Text to synthesize and play."),
            ToolArgument(
                "response_to_event_id",
                "Optional event ID this answers.",
                required=False,
                schema={"type": ["integer", "null"]},
            ),
            ToolArgument(
                "response_kind",
                "Optional structured response kind for runtime playback policy.",
                required=False,
                schema={"type": ["string", "null"]},
            ),
        ),
    ),
    ToolDefinition(
        "hangup_call",
        "Hang up an active call through Asterisk AMI.",
        (
            ToolArgument("call_id", "Active call ID."),
            ToolArgument(
                "response_to_event_id",
                "Optional event ID this answers.",
                required=False,
                schema={"type": ["integer", "null"]},
            ),
        ),
    ),
    ToolDefinition(
        "transfer_call",
        "Transfer an active call to another SIP extension or target.",
        (
            ToolArgument("call_id", "Active call ID."),
            ToolArgument("target", "Extension or SIP target."),
            ToolArgument(
                "response_to_event_id",
                "Optional event ID this answers.",
                required=False,
                schema={"type": ["integer", "null"]},
            ),
        ),
    ),
    ToolDefinition(
        "send_dtmf",
        "Send one DTMF digit into an active call.",
        (
            ToolArgument("call_id", "Active call ID."),
            ToolArgument("digit", "DTMF digit to send.", schema={"type": "string", "minLength": 1, "maxLength": 1}),
            ToolArgument(
                "response_to_event_id",
                "Optional event ID this answers.",
                required=False,
                schema={"type": ["integer", "null"]},
            ),
        ),
    ),
    ToolDefinition(
        "stop_playback",
        "Stop currently queued or playing bot audio in an active call.",
        (
            ToolArgument("call_id", "Active call ID."),
            ToolArgument(
                "reason",
                "Optional reason for stopping playback.",
                required=False,
                schema={"type": ["string", "null"]},
            ),
            ToolArgument(
                "response_to_event_id",
                "Optional event ID this answers.",
                required=False,
                schema={"type": ["integer", "null"]},
            ),
        ),
    ),
    ToolDefinition(
        "delegate_to_subagent",
        "Delegate complex caller work to any registered colleague/subagent provider. Use this generic tool when the provider is chosen by runtime configuration or when a non-FlowHunt provider is registered.",
        (
            ToolArgument("call_id", "Active call ID."),
            ToolArgument("message", "Caller request and relevant context to send to the colleague provider."),
            ToolArgument("provider", "Registered provider kind, for example flowhunt_flow or flowhunt_project."),
            ToolArgument(
                "metadata",
                "Provider-specific metadata such as flow_id, project_id, skill, or target identifiers.",
                required=False,
                schema={"type": ["object", "null"]},
            ),
            ToolArgument(
                "dedupe_key",
                "Optional stable dedupe key. Defaults to the response event id or current request.",
                required=False,
                schema={"type": ["string", "null"]},
            ),
            ToolArgument(
                "response_to_event_id",
                "Optional event ID this answers.",
                required=False,
                schema={"type": ["integer", "null"]},
            ),
            ToolArgument(
                "suppress_progress",
                "When true, do not speak the default progress acknowledgement before scheduling the task.",
                required=False,
                schema={"type": ["boolean", "null"]},
            ),
        ),
    ),
    ToolDefinition(
        "invoke_flowhunt_flow",
        "Invoke a configured FlowHunt Flow for any complex caller request in any language, including research, website checks, comparisons, account work, or tasks needing external tools. Use the caller's actual request as message.",
        (
            ToolArgument("call_id", "Active call ID."),
            ToolArgument("message", "Caller request and relevant context to send to the FlowHunt Flow."),
            ToolArgument(
                "response_to_event_id",
                "Optional event ID this answers.",
                required=False,
                schema={"type": ["integer", "null"]},
            ),
            ToolArgument(
                "suppress_progress",
                "When true, do not speak the default progress acknowledgement before scheduling the task.",
                required=False,
                schema={"type": ["boolean", "null"]},
            ),
        ),
    ),
    ToolDefinition(
        "create_flowhunt_project_issue",
        "Create a FlowHunt AI Project issue for a complex caller request and wait briefly for the agent-team result.",
        (
            ToolArgument("call_id", "Active call ID."),
            ToolArgument("title", "Short issue title."),
            ToolArgument("description", "Caller request and relevant context for the FlowHunt AI Project."),
            ToolArgument(
                "project_id",
                "Optional FlowHunt AI Project ID. Defaults to VOICEBOT_FLOWHUNT_PROJECT_ID.",
                required=False,
                schema={"type": ["string", "null"]},
            ),
            ToolArgument(
                "response_to_event_id",
                "Optional event ID this answers.",
                required=False,
                schema={"type": ["integer", "null"]},
            ),
            ToolArgument(
                "suppress_progress",
                "When true, do not speak the default progress acknowledgement before scheduling the task.",
                required=False,
                schema={"type": ["boolean", "null"]},
            ),
        ),
    ),
    ToolDefinition(
        "list_transcripts",
        "List call IDs with persisted transcripts.",
        (),
    ),
    ToolDefinition(
        "list_transcript_summaries",
        "List persisted transcripts with event counts and first/last timestamps.",
        (
            ToolArgument("after_call_id", "Optional call ID cursor.", required=False, schema={"type": ["string", "null"]}),
            ToolArgument("limit", "Optional maximum number of transcript summaries.", required=False, schema={"type": "integer"}),
        ),
    ),
    ToolDefinition(
        "get_transcript_stats",
        "Read aggregate persisted transcript counts and corruption counters.",
        (
            ToolArgument("after_call_id", "Optional call ID cursor.", required=False, schema={"type": ["string", "null"]}),
            ToolArgument("limit", "Optional maximum number of transcript summaries to aggregate.", required=False, schema={"type": "integer"}),
        ),
    ),
    ToolDefinition(
        "get_transcript",
        "Read the full persisted transcript/events for one call.",
        (
            ToolArgument("call_id", "Call ID."),
            ToolArgument("after", "Optional event ID cursor.", required=False, schema={"type": "integer"}),
            ToolArgument("limit", "Optional maximum number of transcript events.", required=False, schema={"type": "integer"}),
        ),
    ),
    ToolDefinition(
        "get_events",
        "Read recent in-memory events.",
        (
            ToolArgument("after", "Optional event ID cursor.", required=False, schema={"type": "integer"}),
            ToolArgument("call_id", "Optional call filter.", required=False, schema={"type": ["string", "null"]}),
            ToolArgument("limit", "Optional maximum number of events.", required=False, schema={"type": "integer"}),
        ),
    ),
    ToolDefinition(
        "get_metrics",
        "Read aggregated timing and operational metrics.",
        (ToolArgument("call_id", "Optional call filter.", required=False, schema={"type": ["string", "null"]}),),
    ),
    ToolDefinition("get_active_calls", "List currently active call IDs.", ()),
    ToolDefinition("get_call_state", "Read runtime state for one active call.", (ToolArgument("call_id", "Active call ID."),)),
    ToolDefinition("get_runtime_config", "Read redacted runtime configuration.", ()),
    ToolDefinition(
        "get_agent_task_status",
        "Read agent task response and claim status.",
        (ToolArgument("owner", "Optional claim owner filter.", required=False, schema={"type": ["string", "null"]}),),
    ),
    ToolDefinition(
        "get_agent_task_summary",
        "List agent task events with pending, claimed, responded, or inactive state.",
        (
            ToolArgument("after", "Optional event ID cursor.", required=False, schema={"type": "integer"}),
            ToolArgument("call_id", "Optional call filter.", required=False, schema={"type": ["string", "null"]}),
            ToolArgument("owner", "Optional claim owner filter.", required=False, schema={"type": ["string", "null"]}),
            ToolArgument("limit", "Optional maximum number of tasks.", required=False, schema={"type": "integer"}),
        ),
    ),
)


def tool_definitions_legacy() -> list[dict[str, Any]]:
    return [definition.to_legacy_dict() for definition in TOOL_DEFINITIONS]


def tool_definitions_json_schema() -> list[dict[str, Any]]:
    return [definition.to_json_schema() for definition in TOOL_DEFINITIONS]
