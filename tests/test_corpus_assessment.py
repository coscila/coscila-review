from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from ocsl_review.corpus_assessment import (
    AssessmentError, AssessmentValidator, TaskPlanner, build_messages,
    output_schema, plan_escalation, validate_item,
)


def task(task_id="locus-1", *, tier="abstract", complete=True):
    return {
        "id": task_id, "claim": "The study included 60 patients.", "claimType": "numeric",
        "citationLocus": {"id": "citation-1", "bindingStatus": "exact"},
        "context": ["This trial is discussed here."], "citedWork": {"title": "Synthetic trial"},
        "identifiers": {"doi": "10.0000/synthetic"}, "evidenceTier": tier,
        "sourceSha256": "a" * 64, "complete": complete,
        "evidenceUnits": [{"id": "unit-1", "text": "We included 60 patients. Treatment used qPCR."}],
    }


def observation(task_id="locus-1", *, verdict="direct-support", escalate=False):
    return {
        "id": task_id, "supportVerdict": verdict,
        "rationale": "The cited abstract explicitly gives the same count.",
        "evidence": [{"unitId": "unit-1", "quote": "We included 60 patients."}],
        "escalate": escalate, "escalationReason": "The precise claim remains unsettled." if escalate else "",
    }


class CorpusAssessmentTests(unittest.TestCase):
    def test_numeric_and_method_alignment_are_allowed_when_explicitly_quoted(self):
        for claim_type, claim, quote in (
            ("numeric", "The study included 60 patients.", "We included 60 patients."),
            ("method", "The study used qPCR.", "Treatment used qPCR."),
        ):
            t = task()
            t.update(claimType=claim_type, claim=claim)
            o = observation()
            o["evidence"] = [{"unitId": "unit-1", "quote": quote}]
            result = validate_item(t, o)
            self.assertEqual(result["supportVerdict"], "direct-support")
            self.assertEqual(result["evidenceTier"], "abstract")
            self.assertEqual(result["evidenceCoverage"], "complete")

    def test_absent_detail_is_an_accepted_abstention_and_routes_to_full_text(self):
        o = observation(verdict="abstain", escalate=True)
        o["evidence"] = []
        result = validate_item(task(), o)
        self.assertEqual(plan_escalation(result, {"fullTextAvailable": True})["action"], "full-text")
        self.assertEqual(plan_escalation(result, {"fullTextAvailable": False})["action"], "blocked")
        self.assertEqual(plan_escalation(result, {"withinContext": False})["action"], "blocked")
        with self.assertRaisesRegex(AssessmentError, "invalid_escalation"):
            validate_item(task(), observation(verdict="abstain"))

    def test_quoted_excerpt_can_support_but_cannot_prove_absence(self):
        t = task(tier="full-text", complete=False)
        result = validate_item(t, observation())
        self.assertEqual(result["evidenceCoverage"], "excerpt")
        with self.assertRaisesRegex(AssessmentError, "insufficient_coverage"):
            validate_item(t, observation(verdict="not-addressed"))
        t["complete"] = True
        self.assertEqual(validate_item(t, observation(verdict="not-addressed"))["supportVerdict"], "not-addressed")
        with self.assertRaisesRegex(AssessmentError, "insufficient_coverage"):
            validate_item(task(), observation(verdict="not-addressed"))

    def test_partial_support_and_full_text_uncertainty_do_not_trigger_unchanged_replays(self):
        o = observation(verdict="partial-indirect", escalate=True)
        a = validate_item(task(tier="full-text", complete=False), o)
        self.assertEqual(plan_escalation(a, {"moreEvidenceAvailable": True})["action"], "full-text")
        self.assertEqual(plan_escalation(a, {"moreEvidenceAvailable": False})["action"], "blocked")
        a = validate_item(task(tier="full-text"), o)
        self.assertEqual(plan_escalation(a, {"moreEvidenceAvailable": True})["action"], "blocked")
        self.assertEqual(plan_escalation(validate_item(task(), observation()), {})["action"], "none")

    def test_quotes_are_exact_and_bound_to_the_correct_unit(self):
        t = task()
        t["evidenceUnits"].append({"id": "other", "text": "An unrelated observation."})
        for quote in ({"unitId": "other", "quote": "We included 60 patients."},
                      {"unitId": "unit-1", "quote": "We included sixty patients."},
                      {"unitId": "absent", "quote": "We included 60 patients."}):
            o = observation()
            o["evidence"] = [quote]
            with self.assertRaisesRegex(AssessmentError, "invalid_quote") as caught:
                validate_item(t, o)
            self.assertFalse(caught.exception.retryable)

    def test_full_text_uses_cited_units_without_echoing_every_unit_or_confidence(self):
        t = task(tier="full-text")
        t["evidenceUnits"].extend({"id": f"unit-{i}", "text": f"Other section {i}."}
                                  for i in range(2, 535))
        result = validate_item(t, observation())
        self.assertNotIn("readUnitIds", result)
        self.assertNotIn("supportConfidence", result)
        for key, value in (("readUnitIds", ["unit-1"]), ("supportConfidence", 99)):
            o = observation()
            o[key] = value
            with self.assertRaisesRegex(AssessmentError, "invalid_schema"):
                validate_item(t, o)

    def test_incomplete_abstract_is_not_dispatched_or_used_for_support(self):
        t = task(complete=False)
        self.assertEqual(TaskPlanner().plan(t)["action"], "blocked")
        with self.assertRaisesRegex(AssessmentError, "blocked_task"):
            build_messages([t], "abstract")
        with self.assertRaisesRegex(AssessmentError, "insufficient_evidence"):
            validate_item(t, observation())
        self.assertEqual(validate_item(t, observation(verdict="abstain", escalate=True))["supportVerdict"], "abstain")

    def test_batch_preserves_distinct_loci_for_the_same_work_and_evidence(self):
        one, two = task(), task("locus-2")
        two["citationLocus"]["id"] = "citation-2"
        two["claim"] = "The study used qPCR."
        originals = copy.deepcopy([one, two])
        messages = build_messages([one, two], "abstract")
        items = json.loads(messages[1]["content"])["items"]
        self.assertEqual([item["id"] for item in items], ["locus-1", "locus-2"])
        self.assertEqual(items[0]["evidenceDigest"], items[1]["evidenceDigest"])
        self.assertEqual([one, two], originals)
        with self.assertRaisesRegex(AssessmentError, "invalid_batch"):
            build_messages([one, one], "abstract")

    def test_batch_validation_preserves_valid_peers_and_classifies_missing_and_bad_items(self):
        tasks = [task("good"), task("bad"), task("missing")]
        bad = observation("bad")
        bad["evidence"][0]["quote"] = "Fabricated number."
        result = AssessmentValidator().validate_response(tasks, {"items": [observation("good"), bad]})
        self.assertEqual(set(result["accepted"]), {"good"})
        self.assertEqual(result["rejected"]["bad"]["code"], "invalid_quote")
        self.assertFalse(result["rejected"]["bad"]["retryable"])
        self.assertEqual(result["rejected"]["missing"]["code"], "missing_item")
        self.assertTrue(result["rejected"]["missing"]["retryable"])

    def test_duplicate_unknown_and_mismatched_ids_never_replace_another_observation(self):
        result = AssessmentValidator().validate_response([task("one"), task("two")], {
            "items": [observation("one"), observation("one"), observation("two"), observation("unknown")],
        })
        self.assertEqual(set(result["accepted"]), {"two"})
        self.assertEqual(result["rejected"]["one"]["code"], "duplicate_item")
        self.assertEqual(result["unexpected"], ["unknown"])
        with self.assertRaisesRegex(AssessmentError, "wrong_task_id"):
            validate_item(task(), observation("other"))

    def test_task_hash_binds_units_and_evidence_metadata_is_not_model_authored(self):
        t = task()
        before = TaskPlanner().prepare(t)["evidenceDigest"]
        t["evidenceUnits"][0]["text"] += " More evidence."
        self.assertNotEqual(before, TaskPlanner().prepare(t)["evidenceDigest"])
        t["evidenceUnits"].append(copy.deepcopy(t["evidenceUnits"][0]))
        with self.assertRaisesRegex(AssessmentError, "unique"):
            TaskPlanner().prepare(t)

    def test_schema_is_bounded_and_v1_protocol_files_are_not_used_or_modified(self):
        root = Path(__file__).resolve().parents[1]
        old = [root / "src/ocsl_review/census.py", root / "prompts/citation-support-v1.txt",
               root / "schemas/census-assessment.schema.json"]
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in old]
        validate_item(task(), observation())
        schema = output_schema()
        self.assertEqual(schema["properties"]["items"]["maxItems"], 40)
        long = observation()
        long["rationale"] = "x" * 401
        with self.assertRaisesRegex(AssessmentError, "invalid_schema"):
            validate_item(task(), long)
        self.assertEqual(before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in old])


if __name__ == "__main__":
    unittest.main()
