from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from voicebot.stt import OpenAISTTProvider, _stt_language_hint


class OpenAISTTProviderTests(unittest.TestCase):
    def test_auto_language_does_not_force_stt_language_hint(self) -> None:
        self.assertIsNone(_stt_language_hint("auto"))
        self.assertIsNone(_stt_language_hint("multilingual"))
        self.assertEqual(_stt_language_hint("sk"), "sk")

    def test_new_openai_transcribe_models_use_json_response_format(self) -> None:
        provider = OpenAISTTProvider.__new__(OpenAISTTProvider)

        provider._model = "gpt-4o-transcribe"
        self.assertEqual(provider._response_format(), "json")

        provider._model = "gpt-4o-mini-transcribe"
        self.assertEqual(provider._response_format(), "json")

    def test_whisper_keeps_verbose_json_response_format(self) -> None:
        provider = OpenAISTTProvider.__new__(OpenAISTTProvider)
        provider._model = "whisper-1"

        self.assertEqual(provider._response_format(), "verbose_json")

    def test_prompt_summary_echo_is_rejected(self) -> None:
        provider = OpenAISTTProvider.__new__(OpenAISTTProvider)
        provider._prompt = "FlowHunt LiveAgent SIP trunk Asterisk WebRTC OpenAI Anthropic Viktor"

        self.assertTrue(
            provider._looks_like_prompt_echo(
                "Caller mentioned a range of topics including FlowHunt, LiveAgent, SIP trunk, "
                "Asterisk, WebRTC, OpenAI, Anthropic, and Viktor."
            )
        )

    def test_prompt_prefix_echo_is_rejected(self) -> None:
        provider = OpenAISTTProvider.__new__(OpenAISTTProvider)
        provider._prompt = (
            "The caller may speak Slovak, Czech, or English. Common words and product names "
            "include LiveAgent and FlowHunt."
        )

        self.assertTrue(provider._looks_like_prompt_echo("The caller may speak"))

    def test_real_request_with_prompt_term_is_not_prompt_echo(self) -> None:
        provider = OpenAISTTProvider.__new__(OpenAISTTProvider)
        provider._prompt = "FlowHunt LiveAgent SIP trunk Asterisk WebRTC OpenAI Anthropic Viktor"

        self.assertFalse(provider._looks_like_prompt_echo("How many pages has LiveAgent website? Check it with sitemap."))

    def test_prompt_echo_retries_without_prompt(self) -> None:
        provider = OpenAISTTProvider.__new__(OpenAISTTProvider)
        provider._model = "gpt-4o-transcribe"
        provider._language = "en"
        provider._prompt = "FlowHunt LiveAgent SIP trunk Asterisk WebRTC OpenAI Anthropic Viktor"
        provider._min_chars = 2
        provider._provider = "openai"
        provider._lock = _NoopLock()
        calls = []

        class FakeTranscriptions:
            def create(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return SimpleNamespace(
                        text="Caller mentioned a range of topics including FlowHunt, LiveAgent, and Asterisk.",
                        language=None,
                        duration=None,
                        segments=[],
                    )
                return SimpleNamespace(
                    text="How many pages has LiveAgent website? Check it with sitemap.",
                    language=None,
                    duration=None,
                    segments=[],
                )

        provider._client = SimpleNamespace(audio=SimpleNamespace(transcriptions=FakeTranscriptions()))

        with patch("voicebot.stt.wavfile.write"):
            result = provider._transcribe_audio(np.zeros(16, dtype=np.float32), use_prompt=True)

        self.assertEqual(result.text, "How many pages has LiveAgent website? Check it with sitemap.")
        self.assertIn("prompt", calls[0])
        self.assertNotIn("prompt", calls[1])


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None


if __name__ == "__main__":
    unittest.main()
