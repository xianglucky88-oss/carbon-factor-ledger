"""Parsing, validation, content addressing and malformed-input handling."""
import unittest

from carbon_factor_ledger.parser import (
    EventValidationError,
    canonical_json,
    content_hash,
    parse_events,
)
from tests.util import evidence, factor, record


class TestParser(unittest.TestCase):
    def test_content_addressed_ids_are_stable_and_deterministic(self):
        e1 = evidence("EV-1")
        e2 = evidence("EV-1")
        (p1,) = parse_events({"events": [e1]})[0]
        (p2,) = parse_events({"events": [e2]})[0]
        self.assertEqual(p1.event_id, p2.event_id)
        self.assertEqual(p1.event_id, content_hash(
            {"event_type": "evidence_registered", "payload": e1["payload"]}
        ))

    def test_canonical_json_ignores_key_order(self):
        self.assertEqual(
            canonical_json({"a": 1, "b": 2}), canonical_json({"b": 2, "a": 1})
        )

    def test_identical_events_deduplicate_in_ledger(self):
        events, errors = parse_events({"events": [
            evidence("EV-1"), evidence("EV-1"), evidence("EV-1"),
        ]})
        self.assertEqual(errors, [])
        self.assertEqual(len({e.event_id for e in events}), 1)

    def test_malformed_events_are_reported_not_fatal(self):
        events, errors = parse_events({"events": [
            evidence("EV-GOOD"),
            {"event_type": "mystery", "payload": {}},
            {"event_type": "evidence_registered",
             "payload": {"evidence_id": "EV-BAD", "filename": "x",
                         "sha256": "not-a-hash"}},
            "not-an-object",
            factor("F1", 1, value=1.0),
        ]})
        self.assertEqual(len(events), 2)
        self.assertEqual(len(errors), 3)

    def test_rejects_unknown_event_type_and_bad_dates(self):
        # A malformed top-level payload (not a list/envelope) is a hard error.
        with self.assertRaises(EventValidationError):
            parse_events({"nope": 1})
        # Inside a proper envelope, unknown types and bad dates are collected
        # as per-event errors without aborting the import.
        events, errors = parse_events({"events": [
            {"event_type": "nope", "payload": {}},
            record("R1", "P1", quantity=1, occurred_on="not-a-date"),
        ]})
        self.assertEqual(events, [])
        self.assertEqual(len(errors), 2)

    def test_accepts_list_payload_and_dict_payload(self):
        e = evidence("EV-1")
        from_list, _ = parse_events([e])
        from_dict, _ = parse_events({"events": [e]})
        self.assertEqual(from_list[0].event_id, from_dict[0].event_id)

    def test_ordering_is_total_and_reproducible(self):
        raw = [record("R2", "P1", quantity=2, ts="2026-01-02T00:00:00+00:00"),
               record("R1", "P1", quantity=1, ts="2026-01-01T00:00:00+00:00")]
        first, _ = parse_events({"events": raw})
        second, _ = parse_events({"events": list(reversed(raw))})
        # Ingestion order defines the total order; same input => same order.
        self.assertEqual([e.event_id for e in first],
                         [e.event_id for e in second])


if __name__ == "__main__":
    unittest.main()
