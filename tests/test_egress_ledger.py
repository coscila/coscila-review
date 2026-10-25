from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ocsl_review import confidentiality
from ocsl_review.confidentiality import (
    EGRESS_GENESIS,
    _audit_event,
    append_egress_event,
    flush_egress_ledgers,
    read_egress_events,
    verify_egress_ledger,
)
from ocsl_review.errors import ValidationError


class EgressLedgerTests(unittest.TestCase):
    """Append-only JSONL segments with a per-event hash chain."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "chat"
        self.workspace.mkdir()
        self.audit = self.workspace / ".ocsl-review"
        confidentiality._HEADS.clear()
        with confidentiality._SYNC_GUARD:
            if confidentiality._FLUSH_TIMER is not None:
                confidentiality._FLUSH_TIMER.cancel()
            confidentiality._FLUSH_TIMER = None
            confidentiality._UNSYNCED.clear()
            confidentiality._LAST_SYNC.clear()

    def events(self, count: int, start: int = 0) -> list[dict]:
        return [_audit_event("provider", "resolve_reference", {"doi": f"10.1234/{i}"}, decision="allowed-minimized")
                for i in range(start, start + count)]

    def records(self, name: str = "egress-events.jsonl") -> list[dict]:
        return [json.loads(line) for line in (self.audit / name).read_text(encoding="utf-8").splitlines()]

    def test_events_form_a_hash_chain_and_read_back_in_order(self) -> None:
        events = self.events(5)
        for event in events:
            append_egress_event(self.workspace, event)
        records = self.records()
        self.assertEqual([r["index"] for r in records], list(range(5)))
        self.assertEqual(records[0]["prev"], EGRESS_GENESIS)
        self.assertEqual([r["prev"] for r in records[1:]], [r["sha256"] for r in records[:-1]])
        self.assertEqual(read_egress_events(self.workspace), events)
        self.assertEqual(verify_egress_ledger(self.workspace),
                         {"legacyEvents": 0, "chainedEvents": 5, "segments": 1, "headSha256": records[-1]["sha256"]})
        self.assertEqual((self.audit / "egress-events.jsonl").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.audit / "egress-events.lock").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.audit.stat().st_mode & 0o777, 0o700)

    def test_a_legacy_array_stays_readable_and_anchors_the_chain(self) -> None:
        self.audit.mkdir(mode=0o700)
        legacy = self.audit / "egress-events.json"
        body = json.dumps([{"operation": "old-1"}, {"operation": "old-2"}], indent=2).encode()
        legacy.write_bytes(body)
        append_egress_event(self.workspace, {"operation": "new"})
        self.assertEqual(legacy.read_bytes(), body)
        self.assertEqual(self.records()[0]["prev"], hashlib.sha256(body).hexdigest())
        self.assertEqual(read_egress_events(self.workspace),
                         [{"operation": "old-1"}, {"operation": "old-2"}, {"operation": "new"}])
        self.assertEqual(verify_egress_ledger(self.workspace)["legacyEvents"], 2)
        legacy.write_bytes(body.replace(b"old-2", b"old-X"))
        with self.assertRaisesRegex(ValidationError, "hash chain is broken"):
            verify_egress_ledger(self.workspace)

    def test_edited_reordered_or_removed_events_break_verification(self) -> None:
        for event in self.events(4):
            append_egress_event(self.workspace, event)
        path = self.audit / "egress-events.jsonl"
        original = path.read_bytes()
        lines = original.splitlines(keepends=True)
        tampered = {
            "edited": lines[0] + lines[1].replace(b"resolve_reference", b"resolve_reference2") + b"".join(lines[2:]),
            "reordered": lines[1] + lines[0] + b"".join(lines[2:]),
            "removed": lines[0] + b"".join(lines[2:]),
            "reformatted": json.dumps(json.loads(lines[0]), indent=1).replace("\n", " ").encode() + b"\n" + b"".join(lines[1:]),
        }
        for name, body in tampered.items():
            with self.subTest(name=name):
                path.write_bytes(body)
                with self.assertRaisesRegex(ValidationError, "hash chain"):
                    verify_egress_ledger(self.workspace)
        path.write_bytes(original)
        verify_egress_ledger(self.workspace)

    def test_incomplete_tail_is_refused_and_never_rewritten(self) -> None:
        append_egress_event(self.workspace, {"operation": "first"})
        path = self.audit / "egress-events.jsonl"
        damaged = path.read_bytes() + b'{"event":{"operation":"tor'
        path.write_bytes(damaged)
        confidentiality._HEADS.clear()
        with self.assertRaisesRegex(ValidationError, "incomplete record"):
            append_egress_event(self.workspace, {"operation": "second"})
        self.assertEqual(path.read_bytes(), damaged)

    def test_full_segments_are_sealed_read_only_and_the_chain_continues(self) -> None:
        events = self.events(40)
        with patch.object(confidentiality, "MAX_EGRESS_SEGMENT_BYTES", 2000):
            for event in events:
                append_egress_event(self.workspace, event)
        sealed = sorted(p.name for p in self.audit.glob("egress-events.0*.jsonl"))
        self.assertGreater(len(sealed), 2)
        self.assertEqual(sealed[0], "egress-events.000001.jsonl")
        self.assertTrue(all((self.audit / name).stat().st_mode & 0o777 == 0o400 for name in sealed))
        self.assertTrue(all((self.audit / name).stat().st_size <= 2000 for name in sealed))
        first_active = self.records()[0]
        self.assertEqual(first_active["prev"], self.records(sealed[-1])[-1]["sha256"])
        self.assertEqual(read_egress_events(self.workspace), events)
        self.assertEqual(verify_egress_ledger(self.workspace)["segments"], len(sealed) + 1)
        (self.audit / sealed[1]).chmod(0o600)
        (self.audit / sealed[1]).unlink()
        with self.assertRaisesRegex(ValidationError, "numbered contiguously"):
            verify_egress_ledger(self.workspace)

    def test_an_interrupted_seal_is_completed_on_the_next_append(self) -> None:
        for event in self.events(3):
            append_egress_event(self.workspace, event)
        # Crash after linking the sealed name but before unlinking the active name.
        os.link(self.audit / "egress-events.jsonl", self.audit / "egress-events.000001.jsonl")
        append_egress_event(self.workspace, {"operation": "after-crash"})
        self.assertEqual(len(self.records("egress-events.000001.jsonl")), 3)
        self.assertEqual(self.records()[0]["index"], 3)
        self.assertEqual(read_egress_events(self.workspace)[-1], {"operation": "after-crash"})
        self.assertEqual(verify_egress_ledger(self.workspace)["chainedEvents"], 4)

    def test_concurrent_appends_keep_one_contiguous_chain(self) -> None:
        batches = [self.events(100, start=n * 100) for n in range(8)]

        def worker(events: list[dict]) -> None:
            for event in events:
                append_egress_event(self.workspace, event)

        threads = [threading.Thread(target=worker, args=(batch,)) for batch in batches]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        result = verify_egress_ledger(self.workspace)
        self.assertEqual(result["chainedEvents"], 800)
        self.assertEqual(sorted(json.dumps(e, sort_keys=True) for e in read_egress_events(self.workspace)),
                         sorted(json.dumps(e, sort_keys=True) for batch in batches for e in batch))

    def test_sync_runs_at_most_once_per_interval_and_pending_events_flush(self) -> None:
        clock = [1000.0]
        synced, armed = [], []
        with patch.object(confidentiality.time, "monotonic", lambda: clock[0]), \
                patch.object(confidentiality, "_schedule_flush", side_effect=lambda: armed.append(1)), \
                patch.object(confidentiality, "_datasync", side_effect=lambda fd: synced.append(fd)):
            append_egress_event(self.workspace, {"operation": "first"})
            self.assertEqual((len(synced), len(armed)), (1, 0))
            for _ in range(5):
                append_egress_event(self.workspace, {"operation": "burst"})
            self.assertEqual((len(synced), len(armed)), (1, 5))
            flush_egress_ledgers()
            self.assertEqual(len(synced), 2)
            flush_egress_ledgers()
            self.assertEqual(len(synced), 2)
            clock[0] += confidentiality.EGRESS_SYNC_SECONDS
            append_egress_event(self.workspace, {"operation": "later"})
            self.assertEqual(len(synced), 3)

    def test_a_burst_tail_is_flushed_by_the_timer_without_further_events(self) -> None:
        synced = threading.Event()
        real = confidentiality._datasync
        with patch.object(confidentiality, "EGRESS_SYNC_SECONDS", 0.5), \
                patch.object(confidentiality, "_datasync", side_effect=lambda fd: (real(fd), synced.set())):
            append_egress_event(self.workspace, {"operation": "first"})
            synced.clear()
            append_egress_event(self.workspace, {"operation": "burst"})
            self.assertFalse(synced.is_set())
            self.assertTrue(synced.wait(5), "the armed timer flushes the unsynced tail")
        self.assertEqual(confidentiality._UNSYNCED, set())

    def test_record_lines_are_the_canonical_encoding_of_their_fields(self) -> None:
        event = {"operation": "résumé", "nested": {"b": [1, 2.5, None, True], "a": "\u2028"}, "argumentNames": ["doi"]}
        line, digest = confidentiality._encode_record(event, 7, "f" * 64)
        record = json.loads(line)
        self.assertEqual(line, (confidentiality._ledger_canonical(record) + "\n").encode("utf-8"))
        self.assertEqual(digest, confidentiality._record_digest(event, 7, "f" * 64))
        self.assertEqual(confidentiality._decode_record(line[:-1], "fixture")["event"], event)

    def test_oversized_events_are_rejected_before_writing(self) -> None:
        with self.assertRaisesRegex(ValidationError, "record bound"):
            append_egress_event(self.workspace, {"argumentNames": ["x" * confidentiality.MAX_EGRESS_RECORD_BYTES]})
        self.assertEqual(read_egress_events(self.workspace), [])


if __name__ == "__main__":
    unittest.main()
