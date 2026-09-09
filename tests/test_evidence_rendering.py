from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from ocsl_review.audit import build_audit_summary, claim_digest, citation_cluster_digest
from ocsl_review.errors import ValidationError
from ocsl_review.rendering import render_markdown, render_review_package, validate_review_package
from test_audit_confidentiality import graph


def package():
    audit = graph()
    return {
        "schemaVersion": "1.1.0", "packageId": "example-package", "title": "Evidence review", "confidential": True,
        "manuscript": {"manuscriptId": "ms-1", "sourceSha256": audit["manuscript"]["sourceSha256"], "ingestionComplete": True},
        "reviewPlan": {"manuscriptId": "ms-1", "confidential": True, "ingestionComplete": True, "lenses": []},
        "auditGraph": audit, "concerns": [], "revisionLedger": [], "auditSummary": build_audit_summary(audit),
    }


class EvidenceRenderingTests(unittest.TestCase):
    def test_every_locus_and_enum_survives_rendering(self):
        value = package()
        audit = value["auditGraph"]
        cluster = copy.deepcopy(audit["citationClusters"][0])
        cluster["id"] = "cc2"
        audit["citationClusters"].append(cluster)
        audit["claims"][0]["citationClusterIds"].append("cc2")
        audit["verificationLinks"][0]["claimSha256"] = claim_digest(audit["claims"][0])
        link = copy.deepcopy(audit["verificationLinks"][0])
        link.update(id="v2", citationClusterId="cc2", citationClusterSha256=citation_cluster_digest(cluster))
        audit["verificationLinks"].append(link)
        value["auditSummary"] = build_audit_summary(audit)
        text = render_markdown(value)
        self.assertIn("locus cc1", text)
        self.assertIn("locus cc2", text)
        self.assertEqual(2, text.count("direct-support; full-text; not confident"))
        self.assertIn("reference r1", text)

    def test_separate_axes_render_without_global_blocker_override(self):
        value = package()
        value["auditGraph"]["completenessAssessments"][0]["searchRunIds"] = []
        value["auditSummary"] = build_audit_summary(value["auditGraph"])
        text = render_markdown(value)
        self.assertIn("Citation support: **yes**", text)
        self.assertIn("Citation sufficiency: **not-fully-verifiable**", text)

    def test_manifest_hashes_fresh_outputs_and_uses_private_modes(self):
        value = package()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "review"
            paths = render_review_package(value, output)
            manifest = json.loads(paths[".manifest.json"].read_text())
            for artifact in manifest["artifacts"]:
                data = (Path(directory) / artifact["name"]).read_bytes()
                self.assertEqual(artifact["sha256"], hashlib.sha256(data).hexdigest())
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in paths.values()))
            old_hash = manifest["artifacts"][0]["sha256"]
            value["title"] = "Updated evidence review"
            paths = render_review_package(value, output)
            manifest = json.loads(paths[".manifest.json"].read_text())
            self.assertNotEqual(old_hash, manifest["artifacts"][0]["sha256"])
            with zipfile.ZipFile(paths[".docx"]) as docx:
                self.assertIn(b"not confident", docx.read("word/document.xml"))

    def test_partially_assessed_range_keeps_every_reference_in_each_format(self):
        value = package()
        audit = value["auditGraph"]
        for number in range(2, 11):
            reference = copy.deepcopy(audit["references"][0])
            reference.update(id=f"reference-{number:05d}",
                             acquisition={"state": "pending", "attempted": False, "limitations": []})
            audit["references"].append(reference)
        cluster = audit["citationClusters"][0]
        cluster.update(raw="[1-10]", markers=[str(number) for number in range(1, 11)],
                       referenceIds=[reference["id"] for reference in audit["references"]])
        audit["verificationLinks"][0].update(citationClusterSha256=citation_cluster_digest(cluster),
                                             confidenceLabel="very-confident")
        value["auditSummary"] = build_audit_summary(audit)
        with tempfile.TemporaryDirectory() as directory:
            paths = render_review_package(value, Path(directory) / "range-review")
            text = paths[".md"].read_text()
            self.assertEqual(9, text.count("not assessed; acquisition pending; not confident"))
            self.assertIn("direct-support; full-text; very confident", text)
            with zipfile.ZipFile(paths[".docx"]) as document:
                document_text = document.read("word/document.xml").decode()
            for reference in audit["references"]:
                self.assertIn(f"reference {reference['id']}", text)
                self.assertIn(f"reference {reference['id']}", document_text)
            canonical = json.loads(paths[".json"].read_text())
            self.assertEqual("very-confident", canonical["auditGraph"]["verificationLinks"][0]["confidenceLabel"])

    def test_stale_summary_and_orphan_affirmative_summary_are_rejected(self):
        value = package()
        value["auditSummary"]["auditCoverage"]["sourcesFullTextChecked"] = 9
        with self.assertRaisesRegex(ValidationError, "deterministic"):
            validate_review_package(value)
        value = package()
        value["auditGraph"] = None
        with self.assertRaisesRegex(ValidationError, "canonical audit graph"):
            validate_review_package(value)


if __name__ == "__main__":
    unittest.main()
