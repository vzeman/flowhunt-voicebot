from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


AUTO_LANGUAGE_VALUES = {"", "auto", "detect", "detected", "multilingual", "any"}


@dataclass(frozen=True)
class LanguageDetection:
    language: str
    confidence: float
    source: str


def normalize_language_hint(language: str | None) -> str | None:
    if language is None:
        return None
    normalized = language.strip().lower()
    if normalized in AUTO_LANGUAGE_VALUES:
        return None
    return normalized


def is_auto_language(language: str | None) -> bool:
    if language is None:
        return True
    return language.strip().lower() in AUTO_LANGUAGE_VALUES


def detect_text_language(text: str) -> LanguageDetection | None:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    if len(normalized) < 10:
        return None
    if re.search(r"[\u0400-\u04ff]", normalized):
        return LanguageDetection("ru", 0.85, "script")
    if re.search(r"[\uac00-\ud7af]", normalized):
        return LanguageDetection("ko", 0.85, "script")

    slovak_markers = (
        "dobrý deň",
        "dobry den",
        "slovensky",
        "rozprávate",
        "rozpravate",
        "ďakujem",
        "dakujem",
        "prosím",
        "prosim",
        "výpadok",
        "vypadok",
    )
    if any(marker in normalized for marker in slovak_markers):
        return LanguageDetection("sk", 0.95, "text_markers")

    slovak_chars = sum(1 for char in normalized if char in "áäčďéíĺľňóôŕšťúýž")
    czech_only_chars = sum(1 for char in normalized if char in "ěříů")
    if slovak_chars >= 2 and czech_only_chars == 0:
        return LanguageDetection("sk", 0.80, "diacritics")
    if czech_only_chars >= 2:
        return LanguageDetection("cs", 0.80, "diacritics")

    english_markers = {"hello", "hi", "please", "thanks", "thank", "status", "outage", "incident"}
    tokens = set(re.findall(r"[a-z]+", normalized))
    if len(tokens & english_markers) >= 2:
        return LanguageDetection("en", 0.75, "text_markers")
    return None


def detected_session_language(call_events: list[Any]) -> dict[str, Any]:
    dropped_transcript_ids = {
        int(event.data.get("transcript_event_id"))
        for event in call_events
        if getattr(event, "type", "") == "stt_result_dropped" and event.data.get("transcript_event_id") is not None
    }
    stt_language_by_turn: dict[int, str] = {}
    current: LanguageDetection | None = None
    for event in call_events:
        event_type = getattr(event, "type", "")
        data = getattr(event, "data", {}) or {}
        if event_type == "stt_finished":
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            language = normalize_language_hint(str(metadata.get("language") or ""))
            if language and data.get("turn_id") is not None:
                stt_language_by_turn[int(data["turn_id"])] = language
            continue
        if event_type != "user_transcript":
            continue
        if int(getattr(event, "id", 0)) in dropped_transcript_ids or data.get("stale"):
            continue
        text = str(data.get("text") or "")
        detected = None
        if data.get("turn_id") is not None:
            language = stt_language_by_turn.get(int(data["turn_id"]))
            if language:
                detected = LanguageDetection(language, 0.95, "stt_metadata")
        detected = detected or detect_text_language(text)
        if detected is None or detected.confidence < 0.75:
            continue
        if current is not None and current.language != detected.language and detected.confidence < 0.90:
            continue
        current = detected
    if current is None:
        return {}
    return {
        "language": current.language,
        "confidence": current.confidence,
        "source": current.source,
    }
