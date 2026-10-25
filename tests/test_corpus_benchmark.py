# SPDX-License-Identifier: Apache-2.0
"""Smoke-run the opt-in synthetic executor benchmark at a small scale.

Rates depend on the machine, so this test checks that every stage runs end to
end on generated data and reports its measurements, not the thresholds.
"""
from pathlib import Path
import tempfile
import unittest

import benchmark_corpus_executor as benchmark


class CorpusBenchmarkSmokeTests(unittest.TestCase):
    def test_small_synthetic_run_measures_every_stage(self):
        with tempfile.TemporaryDirectory() as parent:
            report = benchmark.run_benchmark(loci=240, egress_events=300, job_items=50, parent=Path(parent))
            self.assertEqual(list(Path(parent).iterdir()), [], "the temporary workspace is removed")
        self.assertEqual((report["corpus"]["loci"], report["prepare"]["tasks"], report["prepare"]["blocked"]), (240, 240, 0))
        self.assertEqual(report["import"]["results"], 240)
        self.assertEqual(report["counts"]["claim_loci"], 240)
        self.assertEqual(report["corpus"]["storage"], "packs", "a new library keeps its artifacts in packs")
        self.assertEqual((report["egress"]["events"], report["egress"]["verifiedEvents"]), (300, 300))
        for stage in ("prepare", "import"):
            self.assertGreater(report[stage]["seconds"], 0)
        self.assertGreater(report["status"]["afterImport"]["medianMilliseconds"], 0)
        self.assertEqual(set(report["acceptance"]), {"prepare", "import", "status", "egress", "thresholds"})
        self.assertEqual(240, report["authorize"]["tasks"])
        self.assertNotIn("branch", report)

    def test_branch_run_measures_a_delta_branch_too(self):
        with tempfile.TemporaryDirectory() as parent:
            report = benchmark.run_benchmark(loci=120, egress_events=50, job_items=40, parent=Path(parent), branch=True)
        branch = report["branch"]
        self.assertEqual((120, 120, 120), (branch["prepare"]["tasks"], branch["import"]["results"],
                                           branch["authorize"]["tasks"]))
        self.assertGreater(branch["status"]["medianMilliseconds"], 0)
        self.assertEqual({"prepare", "import", "status"}, set(report["branchAcceptance"]))


if __name__ == "__main__":
    unittest.main()
