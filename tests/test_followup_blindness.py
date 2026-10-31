# SPDX-License-Identifier: Apache-2.0
"""Blindness of the follow-up audit's files: no study identifier, no reference to earlier work, no carried figures.

The follow-up describes a generic audit of claims and citations in a corpus of review articles. Its strata sizes and
rates come from the run's own data, so none of its files may carry a figure: the lab-facing text (skill, instrument
prompts and schemas, docs, the protocol schema) holds no percentage and no grouped count at all, and every follow-up
file passes the hashed study-identifier scan and the folded phrase scan of the census files.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from test_instruments import forbidden_hits

ROOT = Path(__file__).resolve().parents[1]
FOLLOWUP_GLOBS = ("src/ocsl_review/followup_*.py", "src/ocsl_review/citation_function.py",
                  "src/ocsl_review/numeric_check.py", "src/ocsl_review/claim_match.py",
                  "instruments/citation-function/**/*", "instruments/numeric-check/**/*", "instruments/claim-match/**/*",
                  "skills/ocsl-review-followup-audit/**/*", "docs/followup*.md", "schemas/followup-*.json")
LAB_TEXT_GLOBS = ("instruments/citation-function/**/*", "instruments/numeric-check/**/*", "instruments/claim-match/**/*",
                  "skills/ocsl-review-followup-audit/**/*", "docs/followup*.md", "schemas/followup-*.json")
PHRASES = ("earlier census", "earlier study", "earlier studies", "earlier run", "earlier runs", "earlier run's",
           "earlier population", "earlier manifest", "earlier acquisition", "earlier corpus", "earlier audit",
           "earlier judgments", "earlier scores", "earlier results", "previous census", "prior census",
           "reference census", "reference run", "reference scores", "reference study", "original census",
           "oncology audit", "historical oncology", "private record set", "are comparators", "legacy-original-source",
           "reproduction chat", "september panel", "known retracted")
FIGURES = re.compile(r"\d+(?:[.,]\d+)?\s?%|\b\d{1,3}(?:,\d{3})+\b")


def files(globs) -> list[Path]:
    found = set()
    for pattern in globs:
        found.update(path for path in ROOT.glob(pattern)
                     if path.is_file() and "__pycache__" not in path.parts and path.name != "LICENSE")
    return sorted(found)


class FollowupBlindnessTests(unittest.TestCase):
    def test_the_scans_see_the_follow_up_files(self):
        names = {str(path.relative_to(ROOT)) for path in files(FOLLOWUP_GLOBS)}
        for required in ("src/ocsl_review/followup_schema.py", "src/ocsl_review/followup_branch.py",
                         "instruments/citation-function/1/system-prompt.txt", "docs/followup-audit.md",
                         "skills/ocsl-review-followup-audit/SKILL.md", "schemas/followup-protocol.schema.json"):
            self.assertIn(required, names)

    def test_follow_up_files_carry_no_study_identifiers(self):
        for path in files(FOLLOWUP_GLOBS):
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual([], forbidden_hits(path.read_text(encoding="utf-8")))

    def test_follow_up_files_name_no_earlier_work(self):
        for path in files(FOLLOWUP_GLOBS):
            text = " ".join(path.read_text(encoding="utf-8").lower().split())
            with self.subTest(path=str(path.relative_to(ROOT))):
                for phrase in PHRASES:
                    self.assertNotIn(phrase, text)

    def test_lab_facing_follow_up_text_carries_no_figures(self):
        for path in files(LAB_TEXT_GLOBS):
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual([], FIGURES.findall(path.read_text(encoding="utf-8").replace(" ", " ")
                                                     .replace(" ", " ")))

    def test_the_figure_scan_detects_percentages_and_grouped_counts(self):
        self.assertTrue(FIGURES.search("a share of 3.5 % here"))
        self.assertTrue(FIGURES.search("in 1,234 reviews"))
        self.assertFalse(FIGURES.search("citation-function@1 and S0-S3 in 2025"))


if __name__ == "__main__":
    unittest.main()
