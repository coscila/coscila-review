# SPDX-License-Identifier: Apache-2.0
"""Instrument registry: bundles, digests, pins, the unchanged v2 instrument and blindness of lab-facing files."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocsl_review import corpus_assessment, instruments
from ocsl_review.errors import ValidationError

ROOT = Path(__file__).resolve().parents[1]

# Study-identifying tokens (session, workspace and chat identifiers, file names, outcome figures and interval
# bounds, corpus totals, title fragments and a date seed) are kept here only as sha256 digests, grouped by
# length; lab-facing files, and every tracked file outside tests, must contain none of them at a word start.
# No-break and thin spaces count as spaces, so "12.5<NBSP>%" is the same token as "12.5 %".
FORBIDDEN = {
    4: {"0c90dd551bf12b5fecc7a9c5caa5a728d45b1c2a5f1760aca9331c12ce0df252",
        "c31e0fcedb154b3269a7fbf9b8e71a41d16af95fd4b16cb805df3dcdddf0e700"},
    5: {"0cbe66614a15a187a5aa341c71efafd84d550ff34dc178e773605f28f45cc84b",
        "1a5cf5c59619f15229f4f8c805d0667fd7c2e4001194717fb7ee62582f829045",
        "23c6a973a31b0bd5f99638209767556a02ac34bb793729382b6fd994d1e90b91",
        "486f4110f8bbcf7eda1c0878929785fddddfa708c01ab0a7ef55a79c8ca6d834",
        "787e3c87ea2a0aa33edd0cc62d12ffd6d5c5457f12bbeab3bc9cedeffc06c8e9",
        "844cb8bb059c33fa526cb7124d8c52c6ff309f5113241e4dd16e04f20c9b005d",
        "94957c17d26f4b6175d2162c79e1165bd4071fbba49ae5797c00b1b3131b31b7",
        "a46773d48e58ed47190eba1132d7d735ab2d06fd94859332b8ead7bc2e5163e9",
        "b8ad25220cb9a5bcb26f18dac02f87a1e947b58e5cba443d566601a4e9cd601e",
        "fc17580b56601023f5104f60e79b6e3390def8e30e35cc4fc0ab4ed1a5de3777"},
    6: {"2c1ea11a3e37bce82366e296bbb04a05204ae360b5046b7ebe103682bfbace78",
        "357bd9eef9d277dae8eabee6f41863f6a7489733bafdeb12c7770524a9e51b59",
        "36d383e4f228cc0bdbfcaaf4d78ef76bd7dc2767737a7af341e8c49277be30b7",
        "3e933e14fd7af6bce60513e218c94d1210e2e43eaecb68c911762ace10142a55",
        "47214e5b4a0d19d7eda978b74955ba4e914e0bb4020ecca9dc036241424917d5",
        "d7198881bf92a4872f10e9fa0ef00fb5c8860f56060f85642fcbe1d4ac05a6fd",
        "d88fb831cf45ec772adf0d3aa8537656fe3656b94c06965929d6cfe1d98c7e11"},
    7: {"6736ce4e60cd6dd9edd3bf565368a5cb70c18b0a5c53f4b032f6fc81badd7ff0",
        "9973b513d82595b8bd6b569b85ab24ea7d6793074697276db508ed424124e31a",
        "c0c83edd9625e9bb2d3cd03550dc250197624f743a1a5bd3dad969167bdc8347"},
    8: {"2991c4f2dbfc89e3e183e2afafd355e3901bce7b8c2752b280125ebd5648e769",
        "42474026dbf58ea83c4f5a17a2ce9f74e5690a9ab3b9195e8682d39ad3a17dd2",
        "4636f0a96191eb01b381ce832aa8c719e1a4a92d106067a4102b7534cd7bd48b",
        "7d24b99d06d2eaaacb3962cbd29ebce5c4ccd4c3be5a1ebae44c370ff1d60d5b",
        "7fcf1fa435db30d801a0cabcf6b52a5a01b13c63cd9cf2dd2c2ebe5ce99b20ef",
        "87e405c491a5436773e6386d96c2eabba587a224508a2eda3fcfe4333f3dfb27",
        "a18577d59248e450bed92c174eaa2b1fdeacd73d7b417a819085dd8543c7b3eb",
        "e89c2935a82eb5858fdb7b1ec108c72b5fc288730f5dfd123a53a591a625d4b9",
        "f08b56e242e5cab22bfce9b61d13b457a8f7bf49db4343ff2243a1f72aa76550"},
    9: {"5ae55e00a3e720f2689992cec849487cddbd3903ecabe29db9331004e2912045",
        "ac3fdda654a75afb24caa46cfc59612c03d3daaee4cbfdb94833df7eee51e24e"},
    12: {"2463f01a9b90a2ad08a97f6c854d48e18ced053a7f61906c01e12b4ab438990b"},
    42: {"d8e6d64915d843b393a00b1c75711cf9b95a45cb5ec3ca55f2194600e4daea33"},
    53: {"4a644f4c0b74832906e22a4322b92a083cc26e14fa38b131b03fc46b4c16684e"},
}
# Everything the lab sees: skills (with their assets and references), instruments, prompts, docs and the
# modules that build prompts or runtime output.
LAB_FACING = ["skills", "instruments", "prompts", "docs", "README.md", "schemas/instrument.schema.json",
              "src/ocsl_review/instruments.py", "src/ocsl_review/citation_census.py",
              "src/ocsl_review/census_extraction.py", "src/ocsl_review/census_runner.py",
              "src/ocsl_review/census_broker.py",
              "src/ocsl_review/scores_db.py", "src/ocsl_review/estimands.py", "src/ocsl_review/corpus_facts.py",
              "src/ocsl_review/study_cli.py", "src/ocsl_review/census_abstracts.py"]


SPACES = str.maketrans({"\u00a0": " ", "\u2007": " ", "\u2009": " ", "\u200a": " ", "\u202f": " "})
# Reviewed exclusions from the scan of every tracked file: the tests hold the digests themselves and synthetic
# fixtures, never the tokens.
SCAN_EXCLUDED = ("tests/",)


def tracked_files() -> list[Path]:
    """Every file of the checkout outside the reviewed exclusions (git's index, else a walk of the tree)."""
    import subprocess
    try:
        names = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True,
                               check=True, timeout=30).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                 if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts]
    return [ROOT / name for name in sorted(names) if not name.startswith(SCAN_EXCLUDED) and (ROOT / name).is_file()]


def forbidden_hits(text: str, table=None) -> list[int]:
    text = text.translate(SPACES)
    hits = []
    for start in range(len(text)):
        if start and (text[start - 1].isalnum() or text[start - 1] == "."):
            continue
        for length, digests in (table or FORBIDDEN).items():
            window = text[start:start + length]
            if len(window) == length and hashlib.sha256(window.encode("utf-8")).hexdigest() in digests:
                hits.append(start)
    return hits


class RegistryTests(unittest.TestCase):
    def test_registered_instruments_verify(self):
        self.assertEqual(["citation-census@1", "citation-support@2", "claim-check@1"], instruments.available())
        for instrument_id in instruments.available():
            report = instruments.verify(instruments.load(instrument_id, check=False))
            self.assertTrue(report["ok"], report["problems"])
            self.assertRegex(report["digest"], r"^[0-9a-f]{64}$")

    def test_digest_is_stable_and_covers_files_and_modules(self):
        census = instruments.load("citation-census@1")
        self.assertEqual(census.digest, instruments.load("citation-census@1").digest)
        description = census.describe()
        self.assertEqual({"systemPrompt", "fullTextNote", "labelSchema"}, set(description["files"]))
        self.assertEqual({"validator", "builder"}, set(description["modules"]))
        self.assertEqual(census.manifest["validator"]["sha256"], description["modules"]["validator"])
        self.assertNotEqual(census.digest, instruments.load("citation-support@2").digest)

    def test_bundle_fields(self):
        census = instruments.load("citation-census@1")
        abstract, full = census.tier("abstract"), census.tier("full-text")
        self.assertEqual((40, 0.0, "none", 1), (abstract["geometry"]["itemsPerCall"], abstract["request"]["temperature"],
                                               abstract["request"]["reasoningEffort"], abstract["repair"]["passes"]))
        self.assertEqual(2200, abstract["evidencePolicy"]["abstract"]["cutCharacters"])
        self.assertEqual(3, abstract["evidencePolicy"]["citingSentences"]["max"])
        self.assertEqual((5, "low", 11000), (full["geometry"]["itemsPerCall"], full["request"]["reasoningEffort"],
                                            full["evidencePolicy"]["window"]["characters"]))
        self.assertEqual({"floor": 3000, "perItem": 700}, {k: abstract["request"]["maxOutputTokens"][k] for k in ("floor", "perItem")})
        self.assertEqual("nvidia/Qwen3.8-Flash-Next-NVFP4", census.manifest["requiredModel"]["served"])
        self.assertEqual(20000, census.manifest["sensitivities"]["no-truncation"]["sampleEntries"])
        self.assertEqual(1, census.text("systemPrompt").count("{{journal}}"))
        with self.assertRaises(ValidationError):
            census.tier("panel")

    def test_v2_is_registered_unchanged(self):
        v2 = instruments.load("citation-support@2")
        self.assertEqual(corpus_assessment.PROMPT_PATH.resolve(), v2.file_path("systemPrompt"))
        self.assertEqual(corpus_assessment.SCHEMA_PATH.resolve(), v2.file_path("outputSchema"))
        self.assertEqual(hashlib.sha256(corpus_assessment.PROMPT_PATH.read_bytes()).hexdigest(),
                         v2.manifest["files"]["systemPrompt"]["sha256"])
        self.assertEqual(corpus_assessment.output_schema(), json.loads(v2.text("outputSchema")))
        self.assertEqual("citation-support-v2", corpus_assessment.PROTOCOL_VERSION)
        self.assertEqual(corpus_assessment.MAX_BATCH_ITEMS, v2.tier("abstract")["geometry"]["itemsPerCall"]["max"])
        self.assertEqual("ocsl_review.corpus_assessment", v2.manifest["validator"]["module"])

    def test_ids_and_unknown_instruments(self):
        self.assertEqual(("citation-census", 1), instruments.parse_id("citation-census@1"))
        for bad in ("citation-census", "Census@1", "census@0", "census@x"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                instruments.parse_id(bad)
        with self.assertRaises(ValidationError):
            instruments.load("citation-census@9")
        with self.assertRaises(ValidationError):
            instruments.module_path("os.path")


class TamperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "instruments"
        shutil.copytree(ROOT / "instruments", self.root)
        patcher = mock.patch.object(instruments, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_changed_prompt_fails_verification(self):
        path = self.root / "citation-census/1/system-prompt.txt"
        path.write_bytes(path.read_bytes() + b" ")
        report = instruments.verify(instruments.load("citation-census@1", check=False))
        self.assertFalse(report["ok"])
        self.assertTrue(any("systemPrompt" in p for p in report["problems"]))
        with self.assertRaises(ValidationError):
            instruments.load("citation-census@1")

    def test_changed_settings_change_the_digest(self):
        before = instruments.load("citation-census@1").digest
        path = self.root / "citation-census/1/instrument.json"
        manifest = json.loads(path.read_text())
        changed = copy.deepcopy(manifest)
        changed["tiers"]["abstract"]["geometry"]["itemsPerCall"] = 20
        path.write_text(json.dumps(changed))
        self.assertNotEqual(before, instruments.load("citation-census@1").digest)

    def test_module_pin_is_enforced(self):
        path = self.root / "citation-census/1/instrument.json"
        manifest = json.loads(path.read_text())
        manifest["validator"]["sha256"] = "0" * 64
        path.write_text(json.dumps(manifest))
        report = instruments.verify(instruments.load("citation-census@1", check=False))
        self.assertTrue(any("validator module" in p for p in report["problems"]))

    def test_schema_and_token_count_are_checked(self):
        path = self.root / "citation-census/1/instrument.json"
        manifest = json.loads(path.read_text())
        manifest["unexpected"] = True
        path.write_text(json.dumps(manifest))
        report = instruments.verify(instruments.load("citation-census@1", check=False))
        self.assertTrue(any(p.startswith("schema") for p in report["problems"]))

    def test_paths_cannot_escape(self):
        path = self.root / "citation-census/1/instrument.json"
        manifest = json.loads(path.read_text())
        manifest["files"]["systemPrompt"]["path"] = "../../../pyproject.toml"
        path.write_text(json.dumps(manifest))
        report = instruments.verify(instruments.load("citation-census@1", check=False))
        self.assertFalse(report["ok"])


class StudyBundleTests(unittest.TestCase):
    def test_study_bundle_digest(self):
        census = instruments.load("citation-census@1").digest
        args = dict(protocol={"seed": 7, "window": ["2001-01-01", "2002-12-31"]}, study_prompt="methods live in skills",
                    instruments={"citation-census@1": census}, dataset_versions={"evidence": "a" * 64},
                    package_tags={"ocsl-review": "0.1.0"})
        first = instruments.study_bundle_digest(**args)
        self.assertEqual(first, instruments.study_bundle_digest(**args))
        changed = instruments.study_bundle_digest(**{**args, "protocol": {"seed": 8, "window": ["2001-01-01", "2002-12-31"]}})
        self.assertNotEqual(first["sha256"], changed["sha256"])
        with self.assertRaises(ValidationError):
            instruments.study_bundle_digest(**{**args, "instruments": {"citation-census@1": "short"}})


class BlindnessTests(unittest.TestCase):
    def test_lab_facing_files_carry_no_study_identifiers(self):
        files = []
        for entry in LAB_FACING:
            path = ROOT / entry
            files.extend(sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path])
        self.assertTrue(files)
        for path in files:
            if not path.exists() or "__pycache__" in path.parts:
                continue
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual([], forbidden_hits(path.read_text(encoding="utf-8")))

    def test_every_tracked_file_outside_tests_carries_no_study_identifiers(self):
        files = tracked_files()
        self.assertGreater(len(files), 50)
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual([], forbidden_hits(text))

    def test_bundled_study_prompt_offers_no_path_to_earlier_results(self):
        prompt = (ROOT / "skills/ocsl-review-reference-analysis/assets/meta-meta-literature-review-prompt.md").read_text()
        for phrase in ("import-legacy", "import-snapshot", "existing approval", "run state", "historical scores",
                       "earlier judgments", "original census", "40-item"):
            self.assertNotIn(phrase, prompt)
        lessons = (ROOT / "skills/ocsl-review-reference-analysis/references/census-workflow-lessons.md").read_text()
        for phrase in ("2,200", "11,000", "Forty-item", "first three citing sentences", "earlier scores"):
            self.assertNotIn(phrase, lessons)
        for skill in ("ocsl-review-reference-analysis", "ocsl-review-citation-census"):
            text = (ROOT / "skills" / skill / "SKILL.md").read_text()
            self.assertIn("citation-census@1", text)
            self.assertIn("ocsl-review study", text)

    def test_scanner_detects_tokens_at_word_starts_only(self):
        table = {5: {hashlib.sha256(b"7.7 %").hexdigest()}}
        self.assertTrue(forbidden_hits("a share of 7.7 % here", table))
        self.assertTrue(forbidden_hits("a share of 7.7\u202f% here", table))
        self.assertTrue(forbidden_hits("a share of 7.7\u00a0% here", table))
        self.assertFalse(forbidden_hits("a share of 17.7 % here", table))
        self.assertFalse(forbidden_hits("a share of 1.7.7 % here", table))


if __name__ == "__main__":
    unittest.main()
