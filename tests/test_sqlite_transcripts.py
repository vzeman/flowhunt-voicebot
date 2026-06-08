from __future__ import annotations

import tempfile
import unittest

from voicebot.events import VoicebotEvent
from voicebot.storage import SQLiteTranscriptStore


class SQLiteTranscriptStoreTests(unittest.TestCase):
    def test_reads_summarizes_and_lists_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteTranscriptStore(f"sqlite:///{directory}/transcripts.sqlite3")
            try:
                store.append(VoicebotEvent(1, "call-1", "call_started", "2026-06-08T10:00:00+00:00", {}))
                store.append(VoicebotEvent(2, "call-1", "user_transcript", "2026-06-08T10:00:01+00:00", {"text": "hello"}))
                store.append(VoicebotEvent(3, "call-2", "call_started", "2026-06-08T10:00:02+00:00", {}))
                store.append(VoicebotEvent(4, "system", "metrics", "2026-06-08T10:00:03+00:00", {}))

                self.assertEqual(store.list_call_ids(), ["call-1", "call-2"])
                self.assertEqual([event["id"] for event in store.read("call-1", after=1)], [2])
                self.assertEqual([event["id"] for event in store.read("call-1", limit=1)], [1])
                self.assertEqual(
                    store.summaries(limit=1),
                    [
                        {
                            "call_id": "call-1",
                            "event_count": 2,
                            "first_event_id": 1,
                            "last_event_id": 2,
                            "first_timestamp": "2026-06-08T10:00:00+00:00",
                            "last_timestamp": "2026-06-08T10:00:01+00:00",
                            "skipped_line_count": 0,
                        }
                    ],
                )
                self.assertEqual(
                    store.stats(),
                    {
                        "transcript_count": 2,
                        "event_count": 3,
                        "skipped_line_count": 0,
                        "corrupt_transcript_count": 0,
                        "corrupt_call_ids": [],
                    },
                )
            finally:
                store.close()

    def test_is_idempotent_by_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteTranscriptStore(f"sqlite:///{directory}/transcripts.sqlite3")
            try:
                event = VoicebotEvent(1, "call-1", "call_started", "2026-06-08T10:00:00+00:00", {})
                store.append(event)
                store.append(event)

                self.assertEqual([item["id"] for item in store.read("call-1")], [1])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
