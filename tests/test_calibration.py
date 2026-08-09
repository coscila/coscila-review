from __future__ import annotations

import unittest
from pathlib import Path

from ocsl_review.calibration import evaluate_file, load_jsonl, score_predictions

ROOT = Path(__file__).resolve().parents[1]


class CalibrationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
