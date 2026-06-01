from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from voicebot.config import Settings
from voicebot.stt import HttpBatchSTTProvider, OpenAISTTProvider, _stt_language_hint


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


class HttpBatchSTTProviderTests(unittest.TestCase):
    def test_deepgram_request_uses_native_listen_endpoint(self) -> None:
        provider = HttpBatchSTTProvider(
            Settings(
                stt_provider="deepgram",
                stt_api_key="dg-key",
                stt_model="nova-3",
                language="sk",
            )
        )
        calls = []

        def fake_request(method, url, *, headers, body=None, timeout=0):
            calls.append((method, url, headers, body, timeout))
            return {
                "metadata": {"duration": 1.25, "request_id": "dg-1"},
                "results": {
                    "channels": [
                        {"alternatives": [{"transcript": "Kolko stran ma web?", "confidence": 0.93}]}
                    ]
                },
            }

        with patch("voicebot.stt._json_request", side_effect=fake_request):
            result = provider._transcribe_deepgram(b"wav-bytes")

        self.assertEqual(result.text, "Kolko stran ma web?")
        method, url, headers, body, timeout = calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.startswith("https://api.deepgram.com/v1/listen?"))
        self.assertIn("model=nova-3", url)
        self.assertIn("language=sk", url)
        self.assertEqual(headers["Authorization"], "Token dg-key")
        self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertEqual(body, b"wav-bytes")
        self.assertEqual(timeout, 8.0)
        self.assertEqual(result.metadata["request_id"], "dg-1")

    def test_assemblyai_uploads_and_polls_transcript(self) -> None:
        provider = HttpBatchSTTProvider(
            Settings(
                stt_provider="assemblyai",
                stt_api_key="aai-key",
                stt_model="universal",
                language="en",
                stt_timeout_seconds=3.0,
            )
        )
        calls = []

        def fake_request(method, url, *, headers, body=None, timeout=0):
            calls.append((method, url, headers, body, timeout))
            if url.endswith("/v2/upload"):
                return {"upload_url": "https://cdn.example/audio.wav"}
            if url.endswith("/v2/transcript"):
                return {"id": "tx-1", "status": "queued"}
            if url.endswith("/v2/transcript/tx-1"):
                return {
                    "id": "tx-1",
                    "status": "completed",
                    "text": "How much does it cost?",
                    "language_code": "en",
                    "audio_duration": 1.5,
                }
            raise AssertionError(f"unexpected url {url}")

        with patch("voicebot.stt._json_request", side_effect=fake_request), patch("voicebot.stt.time.sleep"):
            result = provider._transcribe_assemblyai(b"wav-bytes")

        self.assertEqual(result.text, "How much does it cost?")
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "https://api.assemblyai.com/v2/upload")
        self.assertEqual(calls[0][2]["Authorization"], "aai-key")
        self.assertEqual(calls[1][1], "https://api.assemblyai.com/v2/transcript")
        self.assertIn(b"https://cdn.example/audio.wav", calls[1][3])
        self.assertEqual(calls[2][1], "https://api.assemblyai.com/v2/transcript/tx-1")
        self.assertEqual(result.metadata["request_id"], "tx-1")


class _NoopLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None


if __name__ == "__main__":
    unittest.main()
