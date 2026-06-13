from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import hashlib
import re
import threading
from typing import Any, Literal

from .events import EventStore, VoicebotEvent, utc_now
from .subagents import SubagentTask, SubagentTaskRequest


ScopeResolver = Callable[[str], dict[str, str] | None]
CandidateStatus = Literal["pending", "running", "completed", "confirmed", "cancelled", "superseded", "failed"]
ReflectorDecision = Literal["reuse", "wait", "supersede", "cancel"]


@dataclass(frozen=True)
class StreamingRagCandidate:
    call_id: str
    session_id: str
    turn_id: int
    partial_event_id: int
    task_id: str
    workspace_id: str
    voicebot_id: str | None
    provider: str
    source_text: str
    normalized_query: str
    query_hash: str
    status: CandidateStatus = "running"
    triggered_at: str = ""
    submitted_at: str = ""
    first_result_at: str | None = None
    completed_at: str | None = None
    result_fingerprints: tuple[str, ...] = ()
    reflector_decision: ReflectorDecision | None = None
    final_request_event_id: int | None = None


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
        self._by_turn: dict[int, list[StreamingRagCandidate]] = {}

    def observe_partial(self, event: VoicebotEvent) -> SubagentTask | None:
        if not self._enabled():
            return None
        turn_id = _optional_int(event.data.get("turn_id"))
        text = str(event.data.get("text") or "").strip()
        if turn_id is None or not self._eligible_text(text):
            return None

        normalized_query = _normalize(text)
        query_hash = _query_hash(text)
        trigger_mode = self._trigger_mode()
        streaming_enabled = self._streaming_rag_enabled()
        with self._lock:
            candidates = self._by_turn.setdefault(turn_id, [])
            if any(candidate.query_hash == query_hash and candidate.status != "failed" for candidate in candidates):
                return None
            if not streaming_enabled:
                if candidates:
                    return None
            elif trigger_mode == "model_triggered" and candidates:
                newest = candidates[-1]
                if not _meaningfully_new_intent(newest.normalized_query, normalized_query):
                    return None
                for candidate in list(candidates):
                    if candidate.status in {"running", "completed"}:
                        self._cancel_candidate_locked(candidate, reason="superseded_by_new_partial_query", status="superseded")
            elif self._active_count(candidates) >= self._max_parallel():
                return None

        scope = self._scope(event.call_id)
        provider = self._provider()
        if scope is None or provider is None:
            return None

        speculative_key = f"{scope['session_id']}:turn:{turn_id}:{query_hash}"
        triggered_at = event.timestamp or utc_now()
        submitted_at = utc_now()
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
                "normalized_query": normalized_query,
                "query_hash": query_hash,
                "call_id": event.call_id,
                "session_id": scope["session_id"],
                "workspace_id": scope["workspace_id"],
                "voicebot_id": scope.get("voicebot_id"),
                "source": "partial_stt",
                "metadata": event.data.get("metadata") if isinstance(event.data.get("metadata"), dict) else {},
            },
        )
        if streaming_enabled:
            request = replace(
                request,
                metadata={
                    **request.metadata,
                    "streaming_rag_candidate": True,
                    "streaming_rag_trigger_mode": trigger_mode,
                    "streaming_rag_reflector_mode": self._reflector_mode(),
                    "streaming_rag_triggered_at": triggered_at,
                    "streaming_rag_submitted_at": submitted_at,
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

        candidate = StreamingRagCandidate(
            call_id=event.call_id,
            session_id=scope["session_id"],
            turn_id=turn_id,
            partial_event_id=event.id,
            task_id=task.task_id,
            workspace_id=task.workspace_id,
            voicebot_id=scope.get("voicebot_id"),
            provider=task.provider,
            source_text=text,
            normalized_query=normalized_query,
            query_hash=query_hash,
            status="completed" if task.status == "completed" else "running",
            triggered_at=triggered_at,
            submitted_at=submitted_at,
            completed_at=task.updated_at if task.status == "completed" else None,
            result_fingerprints=_task_result_fingerprints(task),
        )
        with self._lock:
            self._by_turn.setdefault(turn_id, []).append(candidate)
        if streaming_enabled:
            self._record_candidate_metric(candidate, "started")
            self._record_speculative_start_metrics(candidate, event)
        return task

    def reconcile_final_request(self, *, turn_id: int | None, final_text: str, final_request_event_id: int) -> SubagentTask | None:
        if not self._enabled() or turn_id is None:
            return None
        with self._lock:
            candidates = list(self._by_turn.get(turn_id, []))
        if not candidates:
            return None

        selected, decision = self._reflect(candidates, final_text)
        if selected is None:
            cancelled = False
            for candidate in list(candidates):
                if candidate.status in {"running", "completed"}:
                    self._cancel_candidate(candidate, reason="final_transcript_changed", status="superseded" if decision == "supersede" else "cancelled")
                    cancelled = True
            if self._streaming_rag_enabled():
                self._record_reflector_decision(turn_id, final_request_event_id, decision, cancelled_candidates=cancelled)
            return None

        try:
            task = self.subagent_coordinator.confirm_speculative(
                selected.task_id,
                selected.workspace_id,
                final_request_event_id=final_request_event_id,
                final_input_text=final_text,
            )
            if self._streaming_rag_enabled():
                self._record_reflector_decision(
                    turn_id,
                    final_request_event_id,
                    decision,
                    task_id=selected.task_id,
                    partial_event_id=selected.partial_event_id,
                )
                task = self._store_candidate_metadata(
                    task,
                    speculative_status="confirmed",
                    streaming_rag_reflector_decision=decision,
                    streaming_rag_final_request_event_id=final_request_event_id,
                    streaming_rag_final_normalized_query=_normalize(final_text),
                )
                self._record_speculative_reuse_metrics(selected, task, final_request_event_id, decision)
            with self._lock:
                self._replace_candidate_locked(
                    replace(
                        selected,
                        status="confirmed",
                        reflector_decision=decision,
                        final_request_event_id=final_request_event_id,
                        completed_at=task.updated_at if task.status == "completed" else selected.completed_at,
                        result_fingerprints=_task_result_fingerprints(task),
                    )
                )
                for candidate in list(self._by_turn.get(turn_id, [])):
                    if candidate.task_id != selected.task_id and candidate.status in {"running", "completed"}:
                        self._cancel_candidate_locked(candidate, reason="not_selected_by_final_reflector", status="superseded")
            return task
        except Exception as exc:
            self.events.append(
                selected.call_id,
                "system",
                {
                    "component": "speculative_turns",
                    "action": "reconcile_final_request",
                    "turn_id": turn_id,
                    "task_id": selected.task_id,
                    "error": str(exc),
                },
            )
            return None

    def cancel_turn(self, turn_id: int | None, *, reason: str) -> SubagentTask | None:
        if not self._enabled() or turn_id is None:
            return None
        with self._lock:
            candidates = [
                candidate
                for candidate in self._by_turn.get(turn_id, [])
                if candidate.status in {"running", "completed"}
            ]
        result = None
        for candidate in candidates:
            result = self._cancel_candidate(candidate, reason=reason, status="cancelled")
        return result

    def _cancel_candidate(
        self,
        candidate: StreamingRagCandidate,
        *,
        reason: str,
        status: CandidateStatus,
    ) -> SubagentTask | None:
        with self._lock:
            return self._cancel_candidate_locked(candidate, reason=reason, status=status)

    def _cancel_candidate_locked(
        self,
        candidate: StreamingRagCandidate,
        *,
        reason: str,
        status: CandidateStatus,
    ) -> SubagentTask | None:
        try:
            if status == "superseded" and hasattr(self.subagent_coordinator, "mark_speculative_superseded"):
                task = self.subagent_coordinator.mark_speculative_superseded(
                    candidate.task_id,
                    candidate.workspace_id,
                    reason=reason,
                )
            else:
                task = self.subagent_coordinator.cancel_speculative(candidate.task_id, candidate.workspace_id, reason=reason)
        except Exception as exc:
            self.events.append(
                candidate.call_id,
                "system",
                {
                    "component": "speculative_turns",
                    "action": "cancel_candidate",
                    "turn_id": candidate.turn_id,
                    "task_id": candidate.task_id,
                    "reason": reason,
                    "error": str(exc),
                },
            )
            self._replace_candidate_locked(replace(candidate, status="failed"))
            return None
        task = self._store_candidate_metadata(
            task,
            speculative_status="superseded" if status == "superseded" else "cancelled",
            streaming_rag_cancel_reason=reason,
        )
        self._replace_candidate_locked(
            replace(
                candidate,
                status=status,
                completed_at=task.updated_at if task.status == "completed" else candidate.completed_at,
                result_fingerprints=_task_result_fingerprints(task),
            )
        )
        if self._streaming_rag_enabled():
            self._record_candidate_metric(candidate, status, reason=reason)
        return task

    def _store_candidate_metadata(self, task: SubagentTask, **metadata: Any) -> SubagentTask:
        try:
            return self.subagent_coordinator.store.update(task.with_metadata(**metadata))
        except Exception:
            return task

    def _replace_candidate_locked(self, replacement: StreamingRagCandidate) -> None:
        candidates = self._by_turn.setdefault(replacement.turn_id, [])
        for index, candidate in enumerate(candidates):
            if candidate.task_id == replacement.task_id:
                candidates[index] = replacement
                return
        candidates.append(replacement)

    def _reflect(
        self,
        candidates: list[StreamingRagCandidate],
        final_text: str,
    ) -> tuple[StreamingRagCandidate | None, ReflectorDecision]:
        if self._reflector_mode() == "disabled":
            return None, "cancel"
        running_or_done = [candidate for candidate in candidates if candidate.status in {"running", "completed", "confirmed"}]
        for candidate in running_or_done:
            if _texts_match(candidate.source_text, final_text):
                task = self.subagent_coordinator.store.get(candidate.task_id, candidate.workspace_id)
                return candidate, "reuse" if task is not None and task.status == "completed" else "wait"
        return None, "supersede" if looks_like_external_work_intent(final_text) else "cancel"

    def _record_reflector_decision(
        self,
        turn_id: int,
        final_request_event_id: int,
        decision: ReflectorDecision,
        *,
        task_id: str | None = None,
        partial_event_id: int | None = None,
        cancelled_candidates: bool = False,
    ) -> None:
        self.events.append(
            self._call_id_for_turn(turn_id),
            "metrics",
            {
                "name": "streaming_rag_reflector_decision",
                "value": 1.0,
                "turn_id": turn_id,
                "decision": decision,
                "final_request_event_id": final_request_event_id,
                "cancelled_candidates": cancelled_candidates,
                **({"task_id": task_id} if task_id else {}),
                **({"partial_event_id": partial_event_id} if partial_event_id is not None else {}),
            },
        )

    def _record_candidate_metric(self, candidate: StreamingRagCandidate, action: str, *, reason: str | None = None) -> None:
        self.events.append(
            candidate.call_id,
            "metrics",
            {
                "name": "streaming_rag_candidate",
                "value": 1.0,
                "action": action,
                "turn_id": candidate.turn_id,
                "task_id": candidate.task_id,
                "partial_event_id": candidate.partial_event_id,
                "query_hash": candidate.query_hash,
                "subagent_provider": candidate.provider,
                "workspace_id": candidate.workspace_id,
                "session_id": candidate.session_id,
                **({"voicebot_id": candidate.voicebot_id} if candidate.voicebot_id else {}),
                **({"reason": reason} if reason else {}),
            },
        )

    def _record_speculative_start_metrics(self, candidate: StreamingRagCandidate, partial_event: VoicebotEvent) -> None:
        partial_to_start = _seconds_between_timestamps(partial_event.timestamp, candidate.submitted_at)
        if partial_to_start is not None:
            self._append_metric(
                candidate.call_id,
                "partial_stt_to_speculative_start_seconds",
                partial_to_start,
                {
                    "turn_id": candidate.turn_id,
                    "partial_event_id": partial_event.id,
                    "task_id": candidate.task_id,
                },
            )
        speech_start = _first_event_for_turn(
            self.events.list_events(call_id=candidate.call_id, limit=1000),
            "user_speech_started",
            candidate.turn_id,
        )
        speech_to_start = _seconds_between_timestamps(speech_start.timestamp if speech_start else "", candidate.submitted_at)
        if speech_to_start is not None:
            self._append_metric(
                candidate.call_id,
                "speech_start_to_speculative_start_seconds",
                speech_to_start,
                {
                    "turn_id": candidate.turn_id,
                    "speech_started_event_id": speech_start.id if speech_start else None,
                    "partial_event_id": partial_event.id,
                    "task_id": candidate.task_id,
                },
            )

    def _record_speculative_reuse_metrics(
        self,
        candidate: StreamingRagCandidate,
        task: SubagentTask,
        final_request_event_id: int,
        decision: ReflectorDecision,
    ) -> None:
        final_event = self.events.get_event(final_request_event_id)
        if final_event is None:
            return
        completed_before_final = task.status == "completed" and _timestamp_lte(task.updated_at, final_event.timestamp)
        self._append_metric(
            candidate.call_id,
            "speculative_task_completed_before_final_transcript",
            1.0 if completed_before_final else 0.0,
            {
                "turn_id": candidate.turn_id,
                "task_id": candidate.task_id,
                "final_request_event_id": final_request_event_id,
                "decision": decision,
            },
        )
        if decision == "reuse" and task.status == "completed":
            reuse_latency = _non_negative_seconds_between(final_event.timestamp, task.updated_at)
            if reuse_latency is not None:
                self._append_metric(
                    candidate.call_id,
                    "speculative_result_reuse_latency_seconds",
                    reuse_latency,
                    {
                        "turn_id": candidate.turn_id,
                        "task_id": candidate.task_id,
                        "final_request_event_id": final_request_event_id,
                    },
                )

    def _append_metric(self, call_id: str, name: str, value: float, data: dict[str, Any]) -> None:
        self.events.append(call_id, "metrics", {"name": name, "value": value, **data})

    def _call_id_for_turn(self, turn_id: int) -> str:
        with self._lock:
            candidates = self._by_turn.get(turn_id) or []
        if candidates:
            return candidates[-1].call_id
        return "system"

    def _enabled(self) -> bool:
        return bool(
            (self._streaming_rag_enabled() or self.settings.speculative_work_enabled)
            and self.subagent_coordinator is not None
        )

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

    def _trigger_mode(self) -> str:
        mode = str(getattr(self.settings, "streaming_rag_trigger_mode", "model_triggered") or "").strip()
        return mode if mode in {"model_triggered", "fixed_interval"} else "model_triggered"

    def _reflector_mode(self) -> str:
        mode = str(getattr(self.settings, "streaming_rag_reflector_mode", "heuristic") or "").strip()
        return mode if mode in {"heuristic", "provider", "disabled"} else "heuristic"

    def _max_parallel(self) -> int:
        if self._streaming_rag_enabled():
            return max(1, int(getattr(self.settings, "streaming_rag_max_parallel_per_turn", 1)))
        return max(1, int(self.settings.speculative_max_per_turn))

    def _active_count(self, candidates: list[StreamingRagCandidate]) -> int:
        return len([candidate for candidate in candidates if candidate.status in {"pending", "running"}])

    def _streaming_rag_enabled(self) -> bool:
        return bool(getattr(self.settings, "streaming_rag_enabled", False))


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


def _meaningfully_new_intent(previous_query: str, next_query: str) -> bool:
    previous = _normalize(previous_query)
    next_normalized = _normalize(next_query)
    if not previous or not next_normalized or previous == next_normalized:
        return False
    previous_tokens = set(_semantic_tokens(previous))
    next_tokens = set(_semantic_tokens(next_normalized))
    added = next_tokens - previous_tokens
    if added:
        return True
    if previous in next_normalized and len(next_normalized) - len(previous) < 12:
        return False
    return not _texts_match(previous, next_normalized)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.:/?-]+", " ", text.lower())).strip()


def _tokens(text: str) -> list[str]:
    return [token for token in _normalize(text).split() if token]


def _semantic_tokens(text: str) -> list[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "can",
        "could",
        "for",
        "i",
        "is",
        "it",
        "me",
        "of",
        "on",
        "or",
        "please",
        "the",
        "to",
        "you",
    }
    return [token for token in _tokens(text) if token not in stopwords]


def _query_hash(text: str) -> str:
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()[:16]


def _task_result_fingerprints(task: SubagentTask) -> tuple[str, ...]:
    values: list[str] = []
    sources: list[dict[str, Any]] = []
    if isinstance(task.metadata, dict):
        sources.append(task.metadata)
    if isinstance(task.provider_references, dict):
        sources.append(task.provider_references)
    if task.result is not None:
        sources.extend([task.result.context, task.result.provider_payload])
    for source in sources:
        for key in ("result_fingerprint", "result_fingerprints", "top_result_ids", "source_ids", "urls"):
            value = source.get(key)
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, (list, tuple)):
                values.extend(str(item) for item in value if item not in (None, ""))
    return tuple(sorted(set(values)))


def _first_event_for_turn(events: list[VoicebotEvent], event_type: str, turn_id: int) -> VoicebotEvent | None:
    for event in events:
        if event.type == event_type and _optional_int(event.data.get("turn_id")) == turn_id:
            return event
    return None


def _seconds_between_timestamps(start_timestamp: str, end_timestamp: str) -> float | None:
    delta = _timestamp_delta_seconds(start_timestamp, end_timestamp)
    return None if delta is None else max(0.0, delta)


def _non_negative_seconds_between(start_timestamp: str, end_timestamp: str) -> float | None:
    delta = _timestamp_delta_seconds(start_timestamp, end_timestamp)
    return None if delta is None else max(0.0, delta)


def _timestamp_lte(left: str, right: str) -> bool:
    try:
        return _parse_timestamp(left) <= _parse_timestamp(right)
    except ValueError:
        return False


def _timestamp_delta_seconds(start_timestamp: str, end_timestamp: str) -> float | None:
    if not start_timestamp or not end_timestamp:
        return None
    try:
        return (_parse_timestamp(end_timestamp) - _parse_timestamp(start_timestamp)).total_seconds()
    except ValueError:
        return None


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
