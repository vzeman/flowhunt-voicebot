from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptDropDecision:
    should_drop: bool
    reason: str | None = None


def should_drop_agent_transcript(
    text: str,
    *,
    stale: bool,
    min_chars: int,
    min_tokens: int,
) -> TranscriptDropDecision:
    if stale:
        return TranscriptDropDecision(True, "stale_transcript")

    tokens = re.findall(r"\w+", text, flags=re.UNICODE)
    alnum_chars = sum(1 for char in text if char.isalnum())
    if alnum_chars < min_chars:
        return TranscriptDropDecision(True, "low_signal_transcript")
    if len(tokens) < min_tokens and alnum_chars <= max(min_chars, 8):
        return TranscriptDropDecision(True, "low_signal_transcript")
    return TranscriptDropDecision(False)
