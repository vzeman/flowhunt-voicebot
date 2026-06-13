from __future__ import annotations

import unittest

from tools.latency_benchmark import run_benchmark


class StreamingRagLatencyBenchmarkTests(unittest.TestCase):
    def test_latency_benchmark_reports_streaming_rag_scenarios(self) -> None:
        result = run_benchmark()

        self.assertTrue(result["ok"])
        self.assertEqual(
            set(result["scenarios"]),
            {
                "final_only",
                "final_tool_only",
                "speculative_confirmed",
                "speculative_cancelled",
                "speculative_superseded",
                "streaming_agent_tts",
            },
        )
        self.assertTrue(result["checks"]["confirmed_speculative_reduces_final_wait"]["ok"])
        self.assertTrue(result["checks"]["speculative_starts_before_endpoint"]["ok"])
        self.assertTrue(result["checks"]["unconfirmed_speculative_result_not_spoken"]["ok"])
        self.assertTrue(result["checks"]["superseded_candidates_reported"]["ok"])

        final_wait = result["scenarios"]["final_tool_only"]["latency"]["slowest_turn"][
            "end_of_speech_to_playback_started_seconds"
        ]
        confirmed_wait = result["scenarios"]["speculative_confirmed"]["latency"]["slowest_turn"][
            "end_of_speech_to_playback_started_seconds"
        ]
        self.assertLess(confirmed_wait, final_wait)

    def test_latency_benchmark_metrics_include_percentiles(self) -> None:
        result = run_benchmark()

        confirmed_metrics = result["scenarios"]["speculative_confirmed"]["metrics"]
        for metric_name in (
            "partial_stt_first_text_seconds",
            "partial_stt_to_speculative_start_seconds",
            "speech_start_to_speculative_start_seconds",
            "speech_finished_to_final_transcript_seconds",
            "speculative_task_completed_before_final_transcript",
            "speculative_result_reuse_latency_seconds",
            "response_request_to_first_playback_seconds",
        ):
            self.assertIn("p50", confirmed_metrics[metric_name])
            self.assertIn("p90", confirmed_metrics[metric_name])

        streaming_metrics = result["scenarios"]["streaming_agent_tts"]["metrics"]
        self.assertEqual(streaming_metrics["agent_stream_first_text_latency_seconds"]["p50"], 0.04)
        self.assertEqual(streaming_metrics["tts_stream_first_audio_latency_seconds"]["p90"], 0.05)
        self.assertEqual(streaming_metrics["response_request_to_first_playback_seconds"]["p50"], 0.12)

    def test_latency_benchmark_reports_speculative_outcomes(self) -> None:
        result = run_benchmark()

        confirmed = result["scenarios"]["speculative_confirmed"]["latency"]["streaming_rag"]
        cancelled = result["scenarios"]["speculative_cancelled"]["latency"]["streaming_rag"]
        superseded = result["scenarios"]["speculative_superseded"]["latency"]["streaming_rag"]

        self.assertEqual(confirmed["confirmed"], 1)
        self.assertEqual(confirmed["confirm_hit_rate"], 1.0)
        self.assertEqual(confirmed["reflector_decisions"], {"reuse": 1})
        self.assertEqual(cancelled["cancelled"], 1)
        self.assertEqual(cancelled["confirm_hit_rate"], 0.0)
        self.assertEqual(superseded["superseded"], 1)
        self.assertEqual(superseded["confirmed"], 1)
        self.assertEqual(superseded["confirm_hit_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
