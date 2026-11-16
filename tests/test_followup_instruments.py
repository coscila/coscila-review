# SPDX-License-Identifier: Apache-2.0
"""The follow-up instruments: registry entries, broker bundles, builders and validators."""
from __future__ import annotations

import copy
import json
import unittest

import followup_fixtures as F
from ocsl_review import citation_function, claim_match, followup_items, instruments, numeric_check
from ocsl_review.followup_items import ItemError
from ocsl_review.followup_model import FOLLOWUP_INSTRUMENTS

MODULES = {"citation-function@1": citation_function, "numeric-check@1": numeric_check, "claim-match@1": claim_match}


def fixture(instrument_id):
    return json.loads((F.FIXTURES / "judge" / (instrument_id.replace("@", "-") + ".json")).read_text(encoding="utf-8"))


class RegistryEntryTests(unittest.TestCase):
    def test_the_shared_builder_module_is_pinned_in_every_follow_up_bundle(self):
        """followup_items is part of all three registry digests; its sha256 is pinned so instruments verify detects an
        accidental change instead of silently producing three new instruments."""
        from ocsl_review import instruments
        for instrument_id in ("citation-function@1", "numeric-check@1", "claim-match@1"):
            with self.subTest(instrument=instrument_id):
                instrument = instruments.load(instrument_id)
                self.assertEqual(instrument.module_digests()["builder"], instrument.manifest["builder"]["sha256"])
                self.assertTrue(instruments.verify(instrument)["ok"])

    def test_bundles_verify_and_export_verbatim_settings(self):
        available = instruments.available()
        for instrument_id in FOLLOWUP_INSTRUMENTS:
            with self.subTest(instrument=instrument_id):
                self.assertIn(instrument_id, available)
                instrument = instruments.load(instrument_id)
                report = instruments.verify(instrument)
                self.assertTrue(report["ok"], report["problems"])
                self.assertEqual("ocsl_review.followup_items", instrument.manifest["builder"]["module"])
                self.assertEqual({"builder", "validator"}, set(instrument.module_digests()))
                (tier,) = instrument.manifest["tiers"]
                spec = instrument.tier(tier)
                self.assertEqual(("none", 0.0, None), (spec["request"]["reasoningEffort"], spec["request"]["temperature"],
                                                      spec["request"]["seed"]))
                self.assertEqual("on-premises judges only", spec["evidencePolicy"]["entitledRouting"])
                exported = instruments.broker_bundle(instrument_id)
                self.assertEqual({"messages": "verbatim"}, exported["bundle"]["transport"])
                self.assertEqual(instrument.text("systemPrompt"), exported["bundle"]["systemPrompt"])

    def test_the_function_judge_is_never_shown_status_or_verdicts(self):
        never = instruments.load("citation-function@1").tier("locus")["evidencePolicy"]["neverSent"]
        for hidden in ("status of the cited work", "stratum", "support verdicts"):
            self.assertIn(hidden, never)
        self.assertFalse({"status", "stratum", "verdict", "provenance"} & set(citation_function.FIELDS))


class BuilderValidatorTests(unittest.TestCase):
    def test_messages_carry_only_the_declared_fields(self):
        for instrument_id, module in MODULES.items():
            data = fixture(instrument_id)
            with self.subTest(instrument=instrument_id):
                items = [{**item, "provenance": "public"} for item in data["items"]]
                messages = module.build_messages(items)
                payload = json.loads(messages[1]["content"])
                self.assertEqual(instruments.load(instrument_id).text("systemPrompt"), messages[0]["content"])
                self.assertEqual(followup_items.PROTOCOL, payload["protocol"])
                for item in payload["items"]:
                    self.assertLessEqual(set(item), set(module.FIELDS))

    def test_fixture_responses_are_accepted(self):
        for instrument_id, module in MODULES.items():
            data = fixture(instrument_id)
            with self.subTest(instrument=instrument_id):
                result = module.validate_response(data["items"], json.dumps(data["response"]))
                self.assertEqual(sorted(item["id"] for item in data["items"]), sorted(result["accepted"]))
                self.assertEqual({}, result["rejected"])
                fenced = "```json\n" + json.dumps(data["response"]) + "\n```"
                self.assertEqual(len(data["items"]), len(module.validate_response(data["items"], fenced)["accepted"]))

    def test_quotes_must_occur_exactly_in_the_named_field(self):
        data = fixture("citation-function@1")
        response = copy.deepcopy(data["response"])
        response["items"][0]["quote"] = "Marker zeta triples the risk"
        response["items"][1]["quoteField"] = "contextAfter"
        result = citation_function.validate_response(data["items"], json.dumps(response))
        self.assertEqual({"locus-1": "invalid_quote", "locus-2": "invalid_quote"},
                         {key: value["code"] for key, value in result["rejected"].items()})
        self.assertTrue(all(value["retryable"] for value in result["rejected"].values()))
        response["items"][1].update(function="relies", quoteField="contextBefore", quote="")
        result = citation_function.validate_response(data["items"], json.dumps(response))
        self.assertEqual("missing_quote", result["rejected"]["locus-2"]["code"])

    def test_abstentions_need_reasons_and_carry_no_class(self):
        data = fixture("citation-function@1")
        response = copy.deepcopy(data["response"])
        response["items"][0].update(function="abstain", quote="", quoteField="", abstainReason="")
        result = citation_function.validate_response(data["items"], json.dumps(response))
        self.assertEqual("missing_reason", result["rejected"]["locus-1"]["code"])
        response["items"][0]["abstainReason"] = "The sentence is cut off."
        result = citation_function.validate_response(data["items"], json.dumps(response))
        row = citation_function.row(result["accepted"]["locus-1"])
        self.assertEqual((None, 0, "abstained"), (row["class"], row["assessable"], row["reason"]))
        self.assertEqual("relies", citation_function.row(data["response"]["items"][0])["class"])

    def test_numeric_parses_must_quote_the_number_as_written(self):
        data = fixture("numeric-check@1")
        response = copy.deepcopy(data["response"])
        response["items"][0]["valueText"] = "12500"
        response["items"][1]["geography"] = "Exampleland"
        result = numeric_check.validate_response(data["items"], json.dumps(response))
        self.assertEqual({"locus-3": "invalid_value", "locus-4": "invalid_parse"},
                         {key: value["code"] for key, value in result["rejected"].items()})

    def test_markers_must_occur_in_their_sentences(self):
        data = fixture("claim-match@1")
        items = copy.deepcopy(data["items"])
        items[0]["candidateMarker"] = "⟨zz⟩"
        with self.assertRaisesRegex(ItemError, "marker"):
            claim_match.build_messages(items)
        function_items = copy.deepcopy(fixture("citation-function@1")["items"])
        function_items[0]["marker"] = "⟨R9⟩"
        with self.assertRaisesRegex(ItemError, "marker"):
            citation_function.build_messages(function_items)

    def test_one_bad_output_never_rejects_the_others(self):
        data = fixture("claim-match@1")
        items = data["items"] + [{**data["items"][0], "id": "locus-1|sentence-2"}]
        result = claim_match.validate_response(items, json.dumps(data["response"]))
        self.assertEqual(["locus-1|sentence-1"], list(result["accepted"]))
        self.assertEqual("missing_item", result["rejected"]["locus-1|sentence-2"]["code"])
        broken = claim_match.validate_response(items, "not json")
        self.assertEqual({"invalid_json"}, {value["code"] for value in broken["rejected"].values()})

    def test_items_that_carry_undeclared_fields_are_refused(self):
        data = fixture("citation-function@1")
        with self.assertRaisesRegex(ItemError, "does not send"):
            citation_function.build_messages([{**data["items"][0], "status": "retracted"}])
        with self.assertRaisesRegex(ItemError, "single tier"):
            citation_function.build_messages(data["items"], "abstract")


if __name__ == "__main__":
    unittest.main()
