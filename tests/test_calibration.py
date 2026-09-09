from __future__ import annotations

import unittest
from pathlib import Path

from ocsl_review.calibration import evaluate_file, load_jsonl, score_predictions, compare_passes
from ocsl_review.errors import ValidationError

ROOT = Path(__file__).resolve().parents[1]


class CalibrationTests(unittest.TestCase):
    def test_empty_case_set_is_blind(self):
        with self.assertRaisesRegex(ValidationError, "blind"):
            score_predictions([], [])

    def test_false_adverse_verdict_is_measured(self):
        gold = load_jsonl(ROOT / "evaluations" / "adversarial" / "cases.jsonl")
        predictions = load_jsonl(ROOT / "evaluations" / "adversarial" / "gold-predictions.jsonl")
        predictions[0]["supportVerdict"] = "contradicted"
        report = score_predictions(gold, predictions)
        self.assertEqual(1, report["falseAdverseVerdicts"])
        self.assertGreater(report["falseAdverseRate"], 0)
        self.assertFalse(report["populationEstimate"])
        self.assertEqual(18, report["metricDenominators"]["supportAccuracy"])

    def test_repair_retains_both_readings_and_direction(self):
        first = [{"caseId": "one", "supportVerdict": "abstain", "severity": "central"}]
        final = [{"caseId": "one", "supportVerdict": "scope-caveat", "severity": "peripheral"}]
        report = compare_passes(first, final)
        self.assertEqual(1, report["towardSupport"])
        self.assertEqual(0, report["agreement"]["severity"])
        self.assertIsNone(report["agreement"]["confidence"])
        self.assertEqual(first[0], report["changes"][0]["before"])
        self.assertIn("not correctness", report["interpretation"])
        with self.assertRaises(ValidationError):
            compare_passes(first, [])

    def test_gold_predictions_score_without_false_support(self):
        report = evaluate_file(ROOT / "evaluations" / "adversarial" / "gold-predictions.jsonl")
        self.assertEqual(report["cases"], 18)
        self.assertEqual(report["falseSupportRate"], 0)
        self.assertEqual(report["scopeMismatchDetection"], 1)
        self.assertEqual(report["citationNeededAccuracy"], 1)
        self.assertEqual(report["publicationStatusRecall"], 1)
        self.assertEqual(report["reviewReportFactuality"], 1)

    def test_false_support_is_the_primary_measured_error(self):
        gold = load_jsonl(ROOT / "evaluations" / "adversarial" / "cases.jsonl")
        predictions = load_jsonl(ROOT / "evaluations" / "adversarial" / "gold-predictions.jsonl")
        predictions[0]["supportVerdict"] = "direct-support"
        report = score_predictions(gold, predictions)
        self.assertGreater(report["falseSupportRate"], 0)
        self.assertEqual(report["falseSupports"], 1)

    def test_missing_metric_denominator_does_not_become_success(self):
        gold = load_jsonl(ROOT / "evaluations" / "adversarial" / "cases.jsonl")
        predictions = load_jsonl(ROOT / "evaluations" / "adversarial" / "gold-predictions.jsonl")
        for prediction in predictions:
            prediction["supportVerdict"] = "abstain"
        report = score_predictions(gold, predictions)
        self.assertIsNone(report["falseSupportRate"])
        self.assertIsNone(report["falseAdverseRate"])
        self.assertEqual(0, report["metricDenominators"]["falseSupportRate"])

    def test_string_truth_values_are_rejected(self):
        gold = load_jsonl(ROOT / "evaluations" / "adversarial" / "cases.jsonl")
        predictions = load_jsonl(ROOT / "evaluations" / "adversarial" / "gold-predictions.jsonl")
        predictions[-1]["reportAssertionSupported"] = "false"
        with self.assertRaisesRegex(ValidationError, "must be boolean"):
            score_predictions(gold, predictions)


if __name__ == "__main__":
    unittest.main()
