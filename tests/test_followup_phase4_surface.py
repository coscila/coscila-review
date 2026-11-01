# SPDX-License-Identifier: Apache-2.0
"""The follow-up leaves the census surface byte-identical.

The census instrument's bundle files, its registry digest and broker bundle digests, its builder and validator module,
the instrument it reuses unchanged (citation-support@2: prompt, output schema, manifest, its validator and builder
module and its registry digest) and the fidelity tests with their
fixtures are pinned here by sha256 as they stand at the follow-up track's base. A change to any of them belongs to the
census's own track and needs a deliberate re-pin here; the follow-up never makes one.
"""
from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from ocsl_review import instruments

ROOT = Path(__file__).resolve().parents[1]
PINNED_FILES = {
    "instruments/citation-census/1/instrument.json": "e7fd4e57a30c6bd42c65815d4ad32c12cadffae8e16520b026b7564fc88ca6d8",
    "instruments/citation-census/1/system-prompt.txt": "e12996191d4e4ad18fe7f21921ff7a3b3f98889031dc5975fff31074c17b9a52",
    "instruments/citation-census/1/full-text-note.txt": "c4e8c46ad014ecdf1ce14fa9f17a9f8f10c9936ad88fee473c7553ba93de8874",
    "instruments/citation-census/1/label.schema.json": "e894d13ba0c0edea2e149fc3e56ab20a238f8da24d008c31cf594fb5ac270d6a",
    "src/ocsl_review/citation_census.py": "f560b4a1e4d4de1bdd5a3d0ef8f990b98dda0da3b9956cffaf23ba860c1ab8c5",
    "prompts/citation-support-v2.txt": "cf075fee7e7aeaad9acd73c25928dcb4f8604fd4c024248983f06e62cab531d6",
    "schemas/corpus-assessment-v2.schema.json": "8547779a0ab620cbc74cf6a25bb015be145a3aa939644524146480aa89521b59",
    "instruments/citation-support/2/instrument.json": "123c1bf66f80c90b17c1ac107d01d1c411e44b0d2b014dc47469380bf6e8e725",
    "src/ocsl_review/corpus_assessment.py": "7c6600397dc60e03980966199d991f3df559640d410ad1bfc12d2746a4d7f4cf",
    "tests/test_citation_census_fidelity.py": "38a29fdbb586820e1e8fd14f7f8dae9536116345331ee2a5a893207c6ed5fc50",
    "tests/fixtures/citation_census/judge_items.json": "cab57af1f2ed281077e8ec5f835132a5aa1d9cb91fab77505354e98da38cbfac",
    "tests/fixtures/citation_census/model_outputs.json": "0235d79332ccce2214c45a369da03883f77c7471aaf6f05c7c71b409a0adcc17",
    "tests/fixtures/citation_census/review_works.json": "d40177abf59567402611b18ac126a3b06962d1121d5c145812abdb2cceafd211",
    "tests/fixtures/citation_census/stage_b_items.json": "597346250944af57f475eee7f57ce35472abaed6dea99e214f1290519caaa356",
    "tests/fixtures/citation_census/review.xml": "e406699f1cf2b5282e98334bf012d6967ae6162c66d359af56561afd7aacc924",
    "tests/fixtures/citation_census/review.html": "d6e26a6f4069880e287bcb8d4216c82dbc90779ec6fbf790c88603ef5f4fa3a6",
    "tests/fixtures/citation_census/review_table_1.html": "6f0dd8e5fb00c8bf76ea749562fa396fd0c3d043539b6bc1d4a154dd46217e0e",
    "tests/fixtures/citation_census/review_table_caption.html":
        "5d33b55f5733be6079a22bc9ed17c6383be3ff982d7203ca1a867728445b4243",
    "tests/fixtures/citation_census/full_texts/ft1.xml": "9191a2081e39254962af8d873131a80375e7efb869018bd834343dab412fa5c7",
    "tests/fixtures/citation_census/full_texts/ft2.xml": "8d4f978849321d9639db960944713b24f7bcd1940922368e847f91e14609acce",
    "tests/fixtures/citation_census/full_texts/ft3.xml": "a1fe83936dbe13de4922615e73c4cb03df057c138c7da3d49f6c25c7aa02358f",
    "tests/fixtures/citation_census/full_texts/ft4.xml": "3610e64eee402bc4f2b68ebd4ebe76ab6b94828d7893434f1404a6f5f325a8f2",
}
CENSUS_DIGEST = "4d1d9c0caa0447c7cd2759bf3bc9cf53115cc4877a4759e4c8097b834687c2f7"
# citation-support@2, reused unchanged: its manifest, its validator and builder module and its registry digest.
SUPPORT_DIGEST = "1d1fe411c80389d9163c414dbddb2d96b1052c199838a17b53b232068ff3fad1"
# Broker bundles of both tiers for a neutral journal parameter (the bytes the execution daemon installs).
BROKER = {
    "abstract": ("acaf22f071bdb80bc7f6907bd39bab701ddafe767207856f8b3f482341b9756e",
                 "94a773b79d7876199fbfb01acb4e20eb79bdfb26d6707e17921c4eebb4a08c99"),
    "full-text": ("ba35f17a34897427b901cf1ac25788ef2313435ca560b3997da96872111bf325",
                  "642fd7f994556017a7d4ffa87e350e7ed6d96305669e7371b8cf4380a8d5b9ef"),
}
NEUTRAL_JOURNAL = "Synthetic Journal of Examples"


class CensusSurfaceTests(unittest.TestCase):
    def test_census_files_are_byte_identical(self):
        for relative, expected in sorted(PINNED_FILES.items()):
            with self.subTest(path=relative):
                self.assertEqual(expected, hashlib.sha256((ROOT / relative).read_bytes()).hexdigest())

    def test_fidelity_fixtures_gained_no_file(self):
        present = sorted(str(path.relative_to(ROOT)) for path in (ROOT / "tests/fixtures/citation_census").rglob("*")
                         if path.is_file() and "__pycache__" not in path.parts)
        self.assertEqual(sorted(key for key in PINNED_FILES if key.startswith("tests/fixtures/citation_census/")), present)

    def test_census_registry_digest_is_unchanged(self):
        census = instruments.load("citation-census@1")
        self.assertEqual(CENSUS_DIGEST, census.digest)
        self.assertEqual("frozen", census.manifest["status"])

    def test_census_broker_bundles_are_unchanged(self):
        for tier, (digest, bundle_sha) in BROKER.items():
            with self.subTest(tier=tier):
                exported = instruments.broker_bundle("citation-census@1", tier=tier,
                                                     parameters={"journal": NEUTRAL_JOURNAL})
                self.assertEqual((digest, bundle_sha), (exported["digest"], exported["bundleSha256"]))

    def test_the_reused_support_instrument_keeps_its_prompt_and_schema(self):
        support = instruments.load("citation-support@2")
        self.assertEqual(SUPPORT_DIGEST, support.digest)
        self.assertEqual("ocsl_review.corpus_assessment", support.manifest["validator"]["module"])
        self.assertEqual(PINNED_FILES["prompts/citation-support-v2.txt"], support.manifest["files"]["systemPrompt"]["sha256"])
        self.assertEqual(PINNED_FILES["schemas/corpus-assessment-v2.schema.json"],
                         support.manifest["files"]["outputSchema"]["sha256"])


if __name__ == "__main__":
    unittest.main()
