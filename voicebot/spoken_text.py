from __future__ import annotations

import re


def clean_spoken_response_text(text: str) -> str:
    cleaned = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"^\s*[-*]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def limit_spoken_response_text(text: str, max_chars: int) -> str:
    cleaned = clean_spoken_response_text(text)
    if max_chars <= 0 or len(cleaned) <= max_chars:
        return cleaned
    truncated = cleaned[:max_chars]
    sentence = re.split(r"(?<=[.!?])\s+", truncated)
    if sentence and len(sentence[0]) >= max_chars * 0.45:
        candidate = sentence[0].strip()
    else:
        candidate = truncated.rsplit(" ", 1)[0].strip()
    candidate = candidate.strip(" -:;,")
    if candidate and candidate[-1] not in ".!?":
        candidate = f"{candidate}."
    return candidate


def split_spoken_response_text(text: str, max_chunk_chars: int) -> list[str]:
    cleaned = clean_spoken_response_text(text)
    if not cleaned:
        return []
    if max_chunk_chars <= 0 or len(cleaned) <= max_chunk_chars:
        return [cleaned]

    chunks: list[str] = []
    for sentence in _sentence_chunks(cleaned):
        if len(sentence) <= max_chunk_chars:
            chunks.append(sentence)
            continue
        chunks.extend(_split_long_sentence(sentence, max_chunk_chars))
    return _merge_tiny_chunks(chunks, max_chunk_chars)


def _sentence_chunks(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]


def _split_long_sentence(sentence: str, max_chunk_chars: int) -> list[str]:
    chunks: list[str] = []
    remaining = sentence.strip()
    while len(remaining) > max_chunk_chars:
        split_at = _best_split_index(remaining, max_chunk_chars)
        chunk = remaining[:split_at].strip(" ,;:")
        if chunk and chunk[-1] not in ".!?":
            chunk = f"{chunk}."
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip(" ,;:")
    if remaining:
        chunks.append(remaining)
    return chunks


def _best_split_index(text: str, max_chunk_chars: int) -> int:
    window = text[:max_chunk_chars]
    for separator in (", ", "; ", ": ", " and ", " or ", " but "):
        index = window.rfind(separator)
        if index >= max(24, int(max_chunk_chars * 0.45)):
            return index + len(separator)
    index = window.rfind(" ")
    if index >= max(16, int(max_chunk_chars * 0.35)):
        return index
    return max_chunk_chars


def _merge_tiny_chunks(chunks: list[str], max_chunk_chars: int) -> list[str]:
    merged: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        if merged and len(chunk) < 28 and len(merged[-1]) + 1 + len(chunk) <= max_chunk_chars:
            merged[-1] = f"{merged[-1]} {chunk}"
        else:
            merged.append(chunk)
    return merged
