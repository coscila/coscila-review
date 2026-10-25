# SPDX-License-Identifier: Apache-2.0
"""The storage acceptance benchmark: a tiny run always, the full-scale run only with OCSL_STORAGE_BENCH=1."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "storage_benchmark.py"
spec = importlib.util.spec_from_file_location("storage_benchmark", SCRIPT)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class StorageBenchmarkTests(unittest.TestCase):
    def measure(self, scale, pack_bytes=4 << 20):
        with tempfile.TemporaryDirectory() as directory:
            return benchmark.measure(Path(directory) / "run", scale=scale, pack_bytes=pack_bytes, workers=2,
                                     progress=lambda event: None)

    def assertAccepted(self, report):
        self.assertLessEqual(report["snapshot"]["manifest_bytes"], 10 * 1024 * 1024)
        self.assertLess(report["snapshot"]["files"], 10 ** 4)
        self.assertEqual(report["snapshot"]["linked_packs"], report["snapshot"]["packs"])
        self.assertTrue(report["branch"]["parent_digest_unchanged"])
        self.assertTrue(report["branch"]["parent_evidence_visible"])
        self.assertLess(report["branch"]["delta_bytes"], report["snapshot"]["database_bytes"])
        self.assertEqual(report["projection"]["gate"]["removedRows"], 0)
        self.assertEqual(report["projection"]["gate"]["vocabularyHits"], 0)
        self.assertEqual(report["projection"]["verify"]["removedRows"], 0)
        self.assertTrue(report["projection"]["source_unchanged"])

    def test_tiny_run_meets_every_acceptance_condition(self):
        report = self.measure(0.0005, pack_bytes=64 * 1024)
        self.assertAccepted(report)
        self.assertGreater(report["snapshot"]["packs"], 1)
        for phase in ("database", "link", "index", "check", "manifest"):
            self.assertIn(phase, report["snapshot"]["phases"])

    @unittest.skipUnless(os.environ.get("OCSL_STORAGE_BENCH") == "1", "set OCSL_STORAGE_BENCH=1 for the full-scale run")
    def test_full_scale_snapshot_and_branch_times(self):
        report = self.measure(float(os.environ.get("OCSL_STORAGE_BENCH_SCALE", "1.0")))
        self.assertAccepted(report)
        self.assertLessEqual(report["snapshot"]["seconds"], 300)
        self.assertLessEqual(report["branch"]["seconds"], 120)


if __name__ == "__main__":
    unittest.main()
