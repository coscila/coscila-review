from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from ocsl_review import literature
from ocsl_review.audit import reference_digest
from ocsl_review.errors import ValidationError
from test_audit_confidentiality import graph
import test_literature as literature_fixtures


class LiteratureAdversarialTests(unittest.TestCase):
    setUp = literature_fixtures.LiteratureTests.setUp
    tearDown = literature_fixtures.LiteratureTests.tearDown
    confirm = literature_fixtures.LiteratureTests.confirm

    def test_incomplete_document_cannot_finish_the_reading_workflow(self):
        self.confirm()
        literature.fetch_literature(self.workspace, self.state, self.graph, self.client)
        source = next(iter(self.state["sources"].values()))
        source["complete"] = False
        for entry in self.state["entries"]:
            literature.record_assessments(self.workspace, self.state, self.graph, {
                "referenceId": entry["referenceId"], "readUnitIds": source["expectedUnitIds"],
            })
        status = literature.literature_status(self.state, self.graph)
        self.assertFalse(status["readingComplete"])
        self.assertEqual(0, status["uniqueFullTextsRead"])

    def test_source_replacement_invalidates_supplementary_completeness(self):
        value = graph()
        reference = copy.deepcopy(value["references"][0])
        reference["id"] = "additional1"
        value["additionalReferences"] = [reference]
        examination = copy.deepcopy(value["sourceExaminations"][0])
        examination.update(id="additional-exam", referenceIds=["additional1"],
                           referenceDigests={"additional1": reference_digest(reference)})
        value["sourceExaminations"].append(examination)
        value["completenessAssessments"][0]["sourceExaminationIds"] = ["additional-exam"]
        literature._invalidate_reference(value, "additional1")
        self.assertEqual([], value["completenessAssessments"])
        self.assertFalse(value["claims"][0]["processed"])

    def test_unmanaged_modern_evidence_cannot_publish_invented_reading(self):
        value = graph()
        body = b"Only unrelated local bytes, with no recorded extraction."
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "source.bin").write_bytes(body)
            examination = value["sourceExaminations"][0]
            examination["artifactPath"] = "source.bin"
            examination["artifactSha256"] = hashlib.sha256(body).hexdigest()
            with self.assertRaises(ValidationError):
                literature.guard_graph(workspace, value)


if __name__ == "__main__":
    unittest.main()
