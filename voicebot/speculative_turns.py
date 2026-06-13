from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import hashlib
import re
import threading
from typing import Any

from .events import EventStore, VoicebotEvent
from .subagents import SubagentTask, SubagentTaskRequest


ScopeResolver = Callable[[str], dict[str, str] | None]


@dataclass(frozen=True)
class SpeculativeTurnState:
    call_id: str
    turn_id: int
    partial_event_id: int
    task_id: str
    workspace_id: str
    provider: str
    partial_text: str
    query_hash: str
    status: str = "started"


class SpeculativeTurnCoordinator:
    def __init__(
        self,
        *,
        settings: Any,
        events: EventStore,
        subagent_coordinator: Any = None,
        subagent_lifecycle: Any = None,
        scope_resolver: ScopeResolver | None = None,
    ) -> None:
        self.settings = settings
        self.events = events
        self.subagent_coordinator = subagent_coordinator
        self.subagent_lifecycle = subagent_lifecycle
        self.scope_resolver = scope_resolver
        self._lock = threading.RLock()
        self._by_turn: dict[int, SpeculativeTurnState] = {}

    def observe_partial(self, event: VoicebotEvent) -> SubagentTask | None:
        if not self._enabled():
            return None
        turn_id = _optional_int(event.data.get("turn_id"))
        text = str(event.data.get("text") or "").strip()
        if turn_id is None or not self._eligible_text(text):
            return None
        with self._lock:
            if turn_id in self._by_turn:
                return None
            if self._turn_count_locked(turn_id) >= max(1, int(self.settings.speculative_max_per_turn)):
                return None

        scope = self._scope(event.call_id)
        provider = self._provider()
        if scope is None or provider is None:
            return None

        query_hash = _query_hash(text)
        speculative_key = f"{scope['session_id']}:turn:{turn_id}:{query_hash}"
        request = SubagentTaskRequest(
            workspace_id=scope["workspace_id"],
            voicebot_id=scope.get("voicebot_id"),
            session_id=scope["session_id"],
            request_event_id=event.id,
            provider=provider,
            input_text=text,
            dedupe_key=f"speculative:{speculative_key}",
            metadata={
                "turn_id": turn_id,
                "partial_event_id": event.id,
                "partial_text": text,
                "query_hash": query_hash,
                "call_id": event.call_id,
                "source": "partial_stt",
                "metadata": event.data.get("metadata") if isinstance(event.data.get("metadata"), dict) else {},
            },
        )
        try:
            task = self.subagent_coordinator.request_speculative(request, speculative_key=speculative_key)
            if self.subagent_lifecycle is not None:
                task = self.subagent_lifecycle.schedule(task)
        except Exception as exc:
            self.events.append(
                event.call_id,
                "system",
                {
                    "component": "speculative_turns",
                    "action": "request_speculative",
                    "turn_id": turn_id,
                    "error": str(exc),
                },
            )
            return None

        with self._lock:
            self._by_turn[turn_id] = SpeculativeTurnState(
                call_id=event.call_id,
                turn_id=turn_id,
                partial_event_id=event.id,
                task_id=task.task_id,
                workspace_id=task.workspace_id,
                provider=task.provider,
                partial_text=text,
                query_hash=query_hash,
            )
        return task

    def reconcile_final_request(self, *, turn_id: int | None, final_text: str, final_request_event_id: int) -> SubagentTask | None:
        if not self._enabled() or turn_id is None:
            return None
        with self._lock:
            state = self._by_turn.get(turn_id)
        if state is None:
            return None

        try:
            if _texts_match(state.partial_text, final_text):
                task = self.subagent_coordinator.confirm_speculative(
                    state.task_id,
                    state.workspace_id,
                    final_request_event_id=final_request_event_id,
                    final_input_text=final_text,
                )
                with self._lock:
                    self._by_turn[turn_id] = replace(state, status="confirmed")
                return task
            return self.cancel_turn(turn_id, reason="final_transcript_changed")
        except Exception as exc:
            self.events.append(
                self._scope_call_id(state),
                "system",
                {
                    "component": "speculative_turns",
                    "action": "reconcile_final_request",
                    "turn_id": turn_id,
                    "task_id": state.task_id,
                    "error": str(exc),
                },
            )
            return None

    def cancel_turn(self, turn_id: int | None, *, reason: str) -> SubagentTask | None:
        if not self._enabled() or turn_id is None:
            return None
        with self._lock:
            state = self._by_turn.get(turn_id)
        if state is None:
            return None
        try:
            task = self.subagent_coordinator.cancel_speculative(state.task_id, state.workspace_id, reason=reason)
        except Exception as exc:
            self.events.append(
                self._scope_call_id(state),
                "system",
                {
                    "component": "speculative_turns",
                    "action": "cancel_turn",
                    "turn_id": turn_id,
                    "task_id": state.task_id,
                    "reason": reason,
                    "error": str(exc),
                },
            )
            return None
        with self._lock:
            self._by_turn[turn_id] = replace(state, status="cancelled")
        return task

    def _enabled(self) -> bool:
        return bool(self.settings.speculative_work_enabled and self.subagent_coordinator is not None)

    def _scope(self, call_id: str) -> dict[str, str] | None:
        if self.scope_resolver is None:
            return None
        scope = self.scope_resolver(call_id)
        if not scope:
            return None
        if not scope.get("workspace_id") or not scope.get("session_id"):
            return None
        return scope

    def _provider(self) -> str | None:
        configured = str(getattr(self.settings, "speculative_subagent_provider", "") or "").strip()
        providers = getattr(self.subagent_coordinator, "providers", {}) if self.subagent_coordinator is not None else {}
        if configured:
            return configured if configured in providers else None
        for preferred in ("flowhunt_project", "flowhunt_flow"):
            if preferred in providers:
                return preferred
        for provider in sorted(providers):
            return str(provider)
        return None

    def _eligible_text(self, text: str) -> bool:
        if len(text) < int(self.settings.speculative_min_chars):
            return False
        if len(_tokens(text)) < int(self.settings.speculative_min_tokens):
            return False
        if not bool(getattr(self.settings, "speculative_external_intent_required", True)):
            return True
        return looks_like_external_work_intent(text)

    def _turn_count_locked(self, turn_id: int) -> int:
        return 1 if turn_id in self._by_turn else 0

    def _scope_call_id(self, state: SpeculativeTurnState) -> str:
        return state.call_id


def looks_like_external_work_intent(text: str) -> bool:
    normalized = _normalize(text)
    if not normalized:
        return False
    intent_patterns = (
        r"\b(check|look up|lookup|search|find|verify|review|analy[sz]e|compare|investigate|research|inspect)\b",
        r"\b(website|web site|url|page|pages|status|pricing|price|account|order|ticket|downtime|incident)\b",
        r"\b(how many|how much|when did|what is the current|latest|today|yesterday)\b",
        r"\bhttps?://|\bwww\.|\.[a-z]{2,}\b",
    )
    return any(re.search(pattern, normalized) for pattern in intent_patterns)


def _texts_match(partial_text: str, final_text: str) -> bool:
    partial = _normalize(partial_text)
    final = _normalize(final_text)
    if not partial or not final:
        return False
    if partial in final or final in partial:
        return True
    partial_tokens = set(_tokens(partial))
    final_tokens = set(_tokens(final))
    if not partial_tokens or not final_tokens:
        return False
    overlap = len(partial_tokens & final_tokens) / max(len(partial_tokens), 1)
    return overlap >= 0.75


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.:/?-]+", " ", text.lower())).strip()


def _tokens(text: str) -> list[str]:
    return [token for token in _normalize(text).split() if token]


def _query_hash(text: str) -> str:
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()[:16]


def _optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
