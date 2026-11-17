"""QA layer 1/4 — activity parsing and input validation oracle."""

import json
import unittest
from pathlib import Path

from carbon_factor_ledger.parser import load_records

from . import generators as G
from .oracles import explain_diff


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "input.json"


class TestActivityParsing(unittest.TestCase):
    def test_fixture_rows_extracted_verbatim(self):
        payload = json.loads(FIXTURE.read_text())
        rows = load_records(payload)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["factor_id"] for r in rows], ["f1", "f2"])
        # copies, not the same object: parser must not hand back live references
        self.assertIsNot(rows[0], payload["activities"][0])
        self.assertEqual(rows[0], payload["activities"][0])

    def test_activities_must_be_list(self):
        for bad in ({"activities": {}}, {"activities": "x"}, {"activities": 42}):
            with self.assertRaises(ValueError):
                load_records(bad)

    def test_missing_activities_is_empty(self):
        self.assertEqual(load_records({}), [])

    def test_non_dict_rows_are_filtered_without_crash(self):
        payload = {"activities": [
            G.make_row("P", 1, 1.0), "junk", 42, None, [],
            G.make_row("Q", 2, 2.0, factor_id="ok"),
        ]}
        rows = load_records(payload)
        self.assertEqual([r["product"] for r in rows], ["P", "Q"])

    def test_empty_and_whitespace_like_payloads(self):
        self.assertEqual(load_records({"activities": []}), [])

    def test_edge_corpus_round_trips_through_parser(self):
        payload = {"activities": G.edge_corpus()}
        rows = load_records(payload)
        self.assertEqual(len(rows), len(payload["activities"]))
        for parsed, original in zip(rows, payload["activities"]):
            self.assertEqual(parsed, original,
                             msg="parser altered an activity\n"
                                 + json.dumps(explain_diff(rows), indent=2))

    def test_illegal_quantities_rejected_later_by_calculation_contract(self):
        # Parser is permissive by design; the numeric contract must fail loudly
        # downstream. This pins *where* responsibility lives.
        for rows in G.illegal_quantity_corpus():
            parsed = load_records({"activities": rows})
            self.assertEqual(len(parsed), 2)
            from carbon_factor_ledger.engine import calculate
            with self.assertRaises((TypeError, ValueError)):
                calculate(parsed, {})

    def test_factor_valid_boundary_key_preservation(self):
        rows = load_records({"activities": G.valid_boundary_corpus()})
        self.assertEqual(
            [r["factor_id"] for r in rows],
            ["missing", "true", "false", "none", "zero", "one",
             "empty-str", "nonempty-str"],
        )
        # raw validity flags survive parsing untouched; engine semantics are
        # tested in the factor layer.
        flags = [r.get("valid", "<missing>") for r in rows]
        self.assertEqual(flags,
                         ["<missing>", True, False, None, 0, 1, "", "yes"])


if __name__ == "__main__":
    unittest.main()
