# SPDX-License-Identifier: Apache-2.0
"""citation-census@1 fidelity: byte-identical prompts, parser results, full-text windows and geometry.

The expected sha256 values below were recorded once by running the original
prompt builders, label parser, JATS conversion, window selection and full-text
call geometry (isolated, offline) on the synthetic fixtures in
tests/fixtures/citation_census. They pin the maintained port to the same bytes.
The tests read inputs only; no verdict data is involved. Journal names are
study inputs.
"""
from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path

from ocsl_review import citation_census as C
from ocsl_review.census_extraction import extract_review
from ocsl_review.errors import ValidationError

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "citation_census"
DOCUMENT_LABELS = ["1", "2", "3", "11", "12", "13", "14", "15"]
REVIEW_TITLE = "Synthetic review of zeta-kinase signalling in breast tumours"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SYSTEM = {
    'Frontiers in Oncology': 'cc16936b6dc1109c7774919e5ca967ded49674366c17dc361084b0f8a9e3a68d',
    'Nature Reviews Clinical Oncology': 'fa860db96863cb796a08de296f330e6cf0e5aa777afe26dc12c4298b082f32c7',
    'Synthetic Journal of Examples': 'a102246cc66a939eb9ecd8e0c1b7e05bdd0f5a84c50dd0e465a4f99205bcdba3',
}
ABSTRACT = {  # (journal, review, batch): (user sha256, message-list sha256)
    ('Frontiers in Oncology', 'paper-a', 0): ('3531ea3982067db2335212321eb420863500094535d79549b7089ae5bb051c4c',
        '7393f26a101401580d1c53efa747fe77ab97f10f3b0f6777939f65dc7de820f4'),
    ('Frontiers in Oncology', 'paper-a', 1): ('0b565fe749506f22b3ff39a3ac38b4aa0c44ed8da2c5857c2b90f1b04e48e45d',
        '9d98d2ddfe71c400752a7ceff001b06a403da5c2a20ba3e6a3f95094b14982b5'),
    ('Frontiers in Oncology', 'paper-b', 0): ('9dada373565d92eb95f08a1d8335e2acc9cc28eca7bd65599a805798e6c6ad72',
        '1480ca1b54edf564297e29e6040f9aae58d0b2871d7d31f4bfd081a416a574d1'),
    ('Nature Reviews Clinical Oncology', 'paper-a', 0): ('3531ea3982067db2335212321eb420863500094535d79549b7089ae5bb051c4c',
        'ae43437125c18100a9cd1b1658f565bc39c7fc5059dd1f0e8d27075940bfdc88'),
    ('Nature Reviews Clinical Oncology', 'paper-a', 1): ('0b565fe749506f22b3ff39a3ac38b4aa0c44ed8da2c5857c2b90f1b04e48e45d',
        'a6556807227e5c859a0eec3568558372b0a15a062bb000b476ec328c1f1c0d6a'),
    ('Nature Reviews Clinical Oncology', 'paper-b', 0): ('9dada373565d92eb95f08a1d8335e2acc9cc28eca7bd65599a805798e6c6ad72',
        'b9433c90b036cafcfef34a33a61e36c20cbf999224090b071d22a22a80589f15'),
    ('Synthetic Journal of Examples', 'paper-a', 0): ('3531ea3982067db2335212321eb420863500094535d79549b7089ae5bb051c4c',
        '5a681b2a0e9f0a64d17dcaaa8d81037b555d7f51c38c8c8572d2423c5c83c90a'),
    ('Synthetic Journal of Examples', 'paper-a', 1): ('0b565fe749506f22b3ff39a3ac38b4aa0c44ed8da2c5857c2b90f1b04e48e45d',
        '9b9a1b649414942a704e46ebb79f1ea8a0f00bd87a62fa25aa0c7ae7ec9c4236'),
    ('Synthetic Journal of Examples', 'paper-b', 0): ('9dada373565d92eb95f08a1d8335e2acc9cc28eca7bd65599a805798e6c6ad72',
        '5974c92212b70622dcc8512101378c04440973fceb0f3897e550eccfa4b26bd7'),
}
REPAIR = {
    ('Frontiers in Oncology', 'paper-a', 0): (('3', '9', '17'), '18d2b3b6453d900619f06d3ed65d2c9fda8513f03576800a3e3a46b3443909ab',
        'cc2a8cf9152976a8c4634578e04e68344c6edf6d40a368dfbc2dacc9aa336aec'),
    ('Frontiers in Oncology', 'paper-a', 1): (('41', '45'), '513a9583370e4721592a17efbe82e32d14ab3f878978d990aa47aec7eda9e3fe',
        'ef46c962d08c977edfc96c21f8f1d4dce2c9d9732b0103a0e7536b2ca0889c11'),
    ('Frontiers in Oncology', 'paper-b', 0): (('2',), 'a214aa3724600e3d6b9368c8f52eaa9ca2456d124914ea0d15a7dc866646fbb2',
        '5dd39995119e8ea89e364874495dd622db2d9728cbff1a2c183a731e3ab851e7'),
    ('Nature Reviews Clinical Oncology', 'paper-a', 0): (('3', '9', '17'), '18d2b3b6453d900619f06d3ed65d2c9fda8513f03576800a3e3a46b3443909ab',
        '6fbd571658095e14385b59facec968f400c08737ed67793d56a97f1590f39302'),
    ('Nature Reviews Clinical Oncology', 'paper-a', 1): (('41', '45'), '513a9583370e4721592a17efbe82e32d14ab3f878978d990aa47aec7eda9e3fe',
        '5f5bf49c1beb8e650da33c856213497ff8cb86ed0ef500779a8ea1a5e885842e'),
    ('Nature Reviews Clinical Oncology', 'paper-b', 0): (('2',), 'a214aa3724600e3d6b9368c8f52eaa9ca2456d124914ea0d15a7dc866646fbb2',
        '39fa7612656ad6059fe6bf220730c87d95e0571c6753d8fa474a36b54bc8bc17'),
    ('Synthetic Journal of Examples', 'paper-a', 0): (('3', '9', '17'), '18d2b3b6453d900619f06d3ed65d2c9fda8513f03576800a3e3a46b3443909ab',
        'c6548df628d776e86ef6059b6b3f41fe7285b55fa4e9d78eede91f9c7b6ea119'),
    ('Synthetic Journal of Examples', 'paper-a', 1): (('41', '45'), '513a9583370e4721592a17efbe82e32d14ab3f878978d990aa47aec7eda9e3fe',
        'a225fd3c830e03c3f7aff3c24406e1b971af102d209b8bdaedbac1d4a1cd975c'),
    ('Synthetic Journal of Examples', 'paper-b', 0): (('2',), 'a214aa3724600e3d6b9368c8f52eaa9ca2456d124914ea0d15a7dc866646fbb2',
        'dc965c0105740e77185f71445db306350c7a6bb483a3addada2f1b6d9d4b87a4'),
}
PARSER = [  # (stage, sha256 of canonical (records, problems, missing)) per output, in fixture order
    ('abstract', 'a5240169db05041e8c8267e42ac2ba4c1e9da0224a1ed45b2aabff2e834858fc'),
    ('fulltext', 'f85ce434f1e3923f6548e00edf62f35a876ecc299a9baa823766898cb09a8d28'),
    ('abstract', '1a82c199c4d1ce2445f88c32c012da4ffb8eb4190b164c50b32651049879b4c7'),
    ('fulltext', 'e6ef2a5fb537029a055dc6432a1b33907094ccaf2331807621486d227d8f226a'),
    ('abstract', '3545f3b5b4bde1784cc355c093863cc97f56e7a36d0e1c49e3e8a1fb35ff77af'),
    ('fulltext', '62a591f06f69a29624070a929618d48a12eb2372468a907fea05510f0447bf96'),
    ('abstract', 'd5d4c294b5d8699dff20e8c2fc0979f54bf0b3a2b5ebc8f2d563b7f4a96665b3'),
    ('fulltext', 'fa4a9af125b3f158e67482659d864d96880a25b066b3e19bb944acb261cf12fd'),
    ('abstract', 'b7ab1264384c6786d0f447bd2a4586e30d19d03a0a7a4eddbae6f66a176af270'),
    ('fulltext', 'b7ab1264384c6786d0f447bd2a4586e30d19d03a0a7a4eddbae6f66a176af270'),
    ('abstract', '8af0ff98ac116daaee9d0f47155b41eb2be440cc82f7eac9735a7a0b3b1e9e50'),
    ('fulltext', '93987f13206536a7428703afe8750decd9ef3bf3f7df1ed76cc37f95b9a01298'),
]
FULL_TEXT = {
    'ft1.xml': 'fe20d3230a996196600c4bf973edf01314ecb2791b64305ed51148c1a1d40a9d',
    'ft2.xml': '2b69d6484789bbfa09f8a1e439980c712f8e90d9066e63ae1b7074cca2e078bf',
    'ft3.xml': '494b169b5a1f5d4cc88bc272c7ccc82d44017caa71e7a6e2b503be0e21d5ff1f',
    'ft4.xml': 'f21ff102acd36202a006f3046e6e642a2badf436b94fe0ed9a68709519d7e715',
}
WINDOW = {
    '4': '525f6e566579e60fb6d74d45d66edc37abc798d477af58dbf8d1edcaf84f573c',
    '9': 'fe20d3230a996196600c4bf973edf01314ecb2791b64305ed51148c1a1d40a9d',
    '12': '494b169b5a1f5d4cc88bc272c7ccc82d44017caa71e7a6e2b503be0e21d5ff1f',
    '15': 'f21ff102acd36202a006f3046e6e642a2badf436b94fe0ed9a68709519d7e715',
    '21': '7f4aa6211dafb6fb5ac828c746bfb2e4093afd090d7268c990bbbef8501e1731',
    '30': 'fe20d3230a996196600c4bf973edf01314ecb2791b64305ed51148c1a1d40a9d',
}
STAGE_B = {
    ('Frontiers in Oncology', 0): ('197141f704a947f42af02e538164d90672ec78438c9346d40167a6239bd859aa',
        '4e440e3c3bb3dbbb0ad290eb86a48f634dbe76c527aeb30b1201f4765f55c10d'),
    ('Frontiers in Oncology', 1): ('977f50831be5613bf84a474f6ba5d32ca41c3a8a64b2661289bb1168007500ac',
        'a4eec8cb8c5c7351715838d7f2ffd35414633e0b74cb6fd11eb96b78dbedfe59'),
    ('Nature Reviews Clinical Oncology', 0): ('197141f704a947f42af02e538164d90672ec78438c9346d40167a6239bd859aa',
        '6bf10ab1e5bf8cb87037ba8777e6de7e6a0330acb42402c43356e4a1ae26f8c4'),
    ('Nature Reviews Clinical Oncology', 1): ('977f50831be5613bf84a474f6ba5d32ca41c3a8a64b2661289bb1168007500ac',
        '35beecd8dc47f5008c16c83ab7ee4eb4282919949e5d835abea9bbd6945929cc'),
    ('Synthetic Journal of Examples', 0): ('197141f704a947f42af02e538164d90672ec78438c9346d40167a6239bd859aa',
        'd5c9de6eeacaed34e93960d2d266d8b14637c768489e5fee0dde8e016505adde'),
    ('Synthetic Journal of Examples', 1): ('977f50831be5613bf84a474f6ba5d32ca41c3a8a64b2661289bb1168007500ac',
        'a801920cc39e628939cfb0cb6ee45ca92232348a961b23341ae16285e2ae265c'),
}
GEOMETRY = {
    7: '0ffafc134f4ce6ef95b0d5083df466a21fd0156044d34edf4708787a5c9a01b6',
    12345: 'c6fc731588242d6240bd1699289d79e34052e98b4fb09fdaa458ed53d88b00b8',
}
DOCUMENT = {
    ('review.xml', 'Frontiers in Oncology'): ('bcd80fb20fd21d9ae4a0b9a34fbd3bdaa5dc8a256373e7f794ae8c6f544ec4cf',
        'd05a912943965b5502fcac911dffa27af359fb5943309453082bcfeaf012ef61'),
    ('review.xml', 'Nature Reviews Clinical Oncology'): ('bcd80fb20fd21d9ae4a0b9a34fbd3bdaa5dc8a256373e7f794ae8c6f544ec4cf',
        '056b9746c9770c865f149fa09c6d711a6efc7a3be0b5f1268614cfd7407c85e8'),
    ('review.html', 'Frontiers in Oncology'): ('a79bdf1c385116d9736f859e0328aa6e894c644265ac5288dd707fa5cfd40d81',
        '275ae2a3dde82865b5f252810fbbf36dc78b9adfb6e95959b98dcdffd40bd4f9'),
    ('review.html', 'Nature Reviews Clinical Oncology'): ('a79bdf1c385116d9736f859e0328aa6e894c644265ac5288dd707fa5cfd40d81',
        '74f0231a845d053ba2721f1e90c79058ba5f724040075c33f46c64992ff5e933'),
}


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class SystemPromptTests(unittest.TestCase):
    def test_system_prompt_digests(self):
        for journal, expected in SYSTEM.items():
            with self.subTest(journal=journal):
                self.assertEqual(expected, sha(C.system_prompt(journal)))

    def test_journal_prompt_prefixes(self):
        self.assertTrue(sha(C.system_prompt("Frontiers in Oncology")).startswith("cc16936b"))
        self.assertTrue(sha(C.system_prompt("Nature Reviews Clinical Oncology")).startswith("fa860db9"))

    def test_journal_parameter_is_validated(self):
        for bad in ("", "  ", "Two\nlines", "{{journal}}", None):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                C.system_prompt(bad)


class AbstractTierFidelityTests(unittest.TestCase):
    def setUp(self):
        self.papers = {paper["key"]: paper for paper in load("judge_items.json")["papers"]}

    def test_first_calls_are_byte_identical(self):
        for (journal, key, index), (user, messages) in ABSTRACT.items():
            paper = self.papers[key]
            batch = C.paper_batches(paper["items"])[index]
            digests = C.digests(C.build_abstract_messages(batch, paper["title"] or "", journal))
            with self.subTest(journal=journal, review=key, batch=index):
                self.assertEqual(user, digests["userSha256"])
                self.assertEqual(messages, digests["messagesSha256"])
                self.assertEqual(SYSTEM[journal], digests["systemSha256"])

    def test_repair_calls_are_byte_identical(self):
        for (journal, key, index), (labels, user, messages) in REPAIR.items():
            paper = self.papers[key]
            batch = C.paper_batches(paper["items"])[index]
            missing = [item for item in batch if item["label"] in labels]
            digests = C.digests(C.build_repair_messages(missing, paper["title"] or "", journal))
            with self.subTest(journal=journal, review=key, batch=index):
                self.assertEqual(user, digests["userSha256"])
                self.assertEqual(messages, digests["messagesSha256"])

    def test_geometry(self):
        paper = self.papers["paper-a"]
        batches = C.paper_batches(paper["items"])
        self.assertEqual([40, 5], [len(batch) for batch in batches])
        self.assertEqual([item["label"] for item in paper["items"]], [i["label"] for b in batches for i in b])
        self.assertEqual(3000, C.max_tokens(1))
        self.assertEqual(3500, C.max_tokens(5))
        self.assertEqual(28000, C.max_tokens(40))
        with self.assertRaises(ValidationError):
            C.max_tokens(0)

    def test_three_passages_and_abstract_cut(self):
        paper = self.papers["paper-a"]
        user = C.build_abstract_messages(paper["items"][:1], paper["title"], "Synthetic Journal of Examples")[1]["content"]
        self.assertEqual(3, user.count("REVIEW SAYS:"))
        self.assertNotIn("never sent", user)
        cut = C.abstract_evidence(paper["items"][8])
        self.assertTrue(cut["cut"])
        self.assertEqual(2200, cut["sentCharacters"])
        exact, over = C.abstract_evidence(paper["items"][9]), C.abstract_evidence(paper["items"][10])
        self.assertEqual((False, True), (exact["cut"], over["cut"]))
        self.assertFalse(C.abstract_evidence(paper["items"][11])["available"])

    def test_no_truncation_sensitivity_changes_only_cut_abstracts(self):
        paper = self.papers["paper-a"]
        batch = C.paper_batches(paper["items"])[0]
        cut = C.build_abstract_messages(batch, paper["title"], "Synthetic Journal of Examples")[1]["content"]
        full = C.build_abstract_messages(batch, paper["title"], "Synthetic Journal of Examples", cut=None)[1]["content"]
        changed = [(a, b) for a, b in zip(cut.split("\n"), full.split("\n")) if a != b]
        self.assertEqual(2, len(changed))
        self.assertTrue(all(a.startswith("ABSTRACT: ") and b.startswith(a) for a, b in changed))
        self.assertEqual(len(cut.split("\n")), len(full.split("\n")))
        self.assertFalse(C.abstract_evidence(batch[8], cut=None)["cut"])

    def test_request_payload_and_body(self):
        messages = C.build_abstract_messages(self.papers["paper-b"]["items"], "", "Synthetic Journal of Examples")
        payload = C.request_payload(messages, model="judge", items=3)
        self.assertEqual(["model", "messages", "max_tokens", "temperature", "stream", "reasoning_effort"], list(payload))
        self.assertEqual((3000, 0.0, False, "none"), (payload["max_tokens"], payload["temperature"], payload["stream"],
                                                     payload["reasoning_effort"]))
        self.assertEqual(json.dumps(payload).encode(), C.request_body(payload))
        full = C.request_payload(messages, model="judge", items=5, tier="full-text")
        self.assertEqual(("low", 3500), (full["reasoning_effort"], full["max_tokens"]))
        digests = C.digests(messages, C.request_body(payload))
        self.assertEqual(hashlib.sha256(C.request_body(payload)).hexdigest(), digests["bodySha256"])


class ParserFidelityTests(unittest.TestCase):
    def test_parser_results_are_identical(self):
        outputs = load("model_outputs.json")["outputs"]
        cases = [(case, stage) for case in outputs for stage in ("abstract", "fulltext")]
        self.assertEqual(len(PARSER), len(cases))
        for (case, stage), (expected_stage, expected) in zip(cases, PARSER):
            parsed = C.parse_labels(case["text"], case["labels"], stage=stage)
            with self.subTest(labels=case["labels"], stage=stage):
                self.assertEqual(expected_stage, stage)
                self.assertEqual(expected, sha(json.dumps(list(parsed), sort_keys=True, ensure_ascii=False)))

    def test_parser_semantics(self):
        outputs = load("model_outputs.json")["outputs"]
        records, problems, missing = C.parse_labels(outputs[0]["text"], outputs[0]["labels"])
        self.assertEqual([], missing)
        self.assertEqual(("true", "direct-support", "confident", 0), tuple(records["1"][k] for k in
                                                                         ("category", "support_verdict", "confidence", "escalate")))
        self.assertEqual(("false", "an invented association"), (records["2"]["category"], records["2"]["quote"]))
        self.assertEqual(("not_assessable", 1, "not-confident"), (records["3"]["category"], records["3"]["escalate"],
                                                                  records["3"]["confidence"]))
        records, problems, missing = C.parse_labels(outputs[1]["text"], outputs[1]["labels"])
        self.assertEqual(["7", "8"], missing)
        self.assertEqual("", records["4"]["quote"])
        self.assertEqual(1, records["5"]["quote_missing"])
        self.assertEqual(("unsure", "unresolved", "abstain"), (records["6"]["category"], records["6"]["resolution_status"],
                                                              records["6"]["support_verdict"]))
        self.assertTrue(any(p.startswith("unexpected label") for p in problems))
        self.assertTrue(any(p.startswith("unparseable") for p in problems))

    def test_malformed_values_are_problems_not_crashes(self):
        records, problems, missing = C.parse_labels('{"l":"1","c":"F","q":7,"e":0,"r":"x"}', ["1"])
        self.assertEqual({}, records)
        self.assertEqual(["1"], missing)
        self.assertTrue(problems and problems[0].startswith("unparseable"))


class FullTextFidelityTests(unittest.TestCase):
    def test_full_text_conversion_and_windows(self):
        texts = {}
        for name, expected in FULL_TEXT.items():
            texts[name] = C.jats_to_text((FIXTURES / "full_texts" / name).read_bytes())
            with self.subTest(document=name):
                self.assertEqual(expected, sha(texts[name]))
        stage_b = load("stage_b_items.json")
        built = []
        for item in stage_b["items"]:
            passages = item["citing_passages"][:3]
            window = C.full_text_window(texts[item["full_text"]], C.claim_text(passages))
            with self.subTest(label=item["label"]):
                self.assertEqual(WINDOW[item["label"]], sha(window))
                self.assertLessEqual(len(window), C.window_characters())
            built.append({"label": item["label"], "citing_passages": passages, "cited_work": item["cited_work"],
                          "fulltext_window": window})
        for (journal, index), (user, messages) in STAGE_B.items():
            digests = C.digests(C.build_full_text_messages(built[index * 5:(index + 1) * 5], stage_b["title"], journal))
            with self.subTest(journal=journal, call=index):
                self.assertEqual(user, digests["userSha256"])
                self.assertEqual(messages, digests["messagesSha256"])

    def test_full_text_geometry(self):
        escalations = [{"paper_key": "p%d" % (i % 7), "label": str(i)} for i in range(1, 64)]
        for seed, expected in GEOMETRY.items():
            calls = C.full_text_calls(escalations, seed, review=lambda e: e["paper_key"],
                                      retrievable=lambda e: e["label"] not in {"5", "11", "40"})
            rendered = [[key] + [e["label"] for e in group] for key, group in calls]
            with self.subTest(seed=seed):
                self.assertEqual(expected, sha(json.dumps(rendered)))
                self.assertTrue(all(1 <= len(group) <= 5 for _, group in calls))
        with self.assertRaises(ValidationError):
            C.full_text_calls(escalations, "seed", review=lambda e: e["paper_key"], retrievable=lambda e: True)

    def test_entity_declarations_are_refused(self):
        with self.assertRaises(ValidationError):
            C.jats_to_text(b'<!DOCTYPE a [<!ENTITY x "y">]><article><body><p>&x;</p></body></article>')
        with self.assertRaises(ValidationError):
            C.jats_to_text(b"<article><body><p>unclosed</body></article>")


class DocumentFidelityTests(unittest.TestCase):
    """Prompts built from fixture documents through the maintained extraction."""

    def test_document_prompts_are_byte_identical(self):
        works = {w["label"]: w for w in load("review_works.json")["works"].values()}
        for name, html in (("review.xml", False), ("review.html", True)):
            review = extract_review((FIXTURES / name).read_bytes(), html=html)
            entries = {entry.label: entry for entry in review["entries"]}
            items = [{"label": label, "citing_passages": entries[label].passages(3),
                      "cited_work": {k: works[label][k] for k in ("title", "venue", "year", "retracted")},
                      "abstract": works[label]["abstract"] or ""} for label in DOCUMENT_LABELS]
            for journal in ("Frontiers in Oncology", "Nature Reviews Clinical Oncology"):
                user, messages = DOCUMENT[(name, journal)]
                digests = C.digests(C.build_abstract_messages(items, REVIEW_TITLE, journal))
                with self.subTest(document=name, journal=journal):
                    self.assertEqual(user, digests["userSha256"])
                    self.assertEqual(messages, digests["messagesSha256"])


class ClassMapTests(unittest.TestCase):
    def test_three_class_map(self):
        self.assertEqual("sure_supported", C.abstract_class("true", 0))
        self.assertEqual("sure_not_supported", C.abstract_class("false", 0))
        for category, escalate in (("true", 1), ("false", 1), ("not_assessable", 0), ("unsure", 0), (None, 0)):
            self.assertEqual("unsure", C.abstract_class(category, escalate))
        self.assertEqual("not_cited", C.abstract_class("false", 0, cited=False))
        self.assertEqual("unsure", C.abstract_class("false", 0, identity_resolved=False))
        self.assertEqual("unsure", C.abstract_class("true", 0, model_judged=False))
        self.assertEqual(["sure_supported", "sure_not_supported", "unsure", "unsure"],
                         [C.full_text_class(c) for c in ("true", "false", "not_assessable", None)])

    def test_deterministic_rows(self):
        never = C.deterministic_record(cited=False, identity_resolved=True)
        self.assertEqual(("not_cited", None), (never["class"], never["category"]))
        unresolved = C.deterministic_record(cited=True, identity_resolved=False)
        self.assertEqual(("unsure", "unsure", "unresolved"), (unresolved["class"], unresolved["category"],
                                                              unresolved["resolution_status"]))
        self.assertIsNone(C.deterministic_record(cited=True, identity_resolved=True))

    def test_quote_check(self):
        self.assertEqual({"quotePresent": None, "quoteInEvidence": None}, C.quote_check({"category": "true"}, "x"))
        self.assertEqual({"quotePresent": False, "quoteInEvidence": False}, C.quote_check({"category": "false", "quote": ""}, "x"))
        self.assertTrue(C.quote_check({"category": "false", "quote": "An  Invented"}, "an invented association")["quoteInEvidence"])


EMPTY_SHA256 = sha("")
DIGESTS = ("systemSha256", "userSha256", "messagesSha256")


def check_record(record: dict) -> str:
    """'checked' when every digest a record carries matches the rebuilt call; 'unusable' for an empty recorded prompt.

    A record carries any of systemSha256, userSha256 and messagesSha256 (sha256 of the JSON message list,
    ensure_ascii=False). A user digest equal to the digest of the empty string means that no prompt was
    rebuilt when the record was made; such a record cannot check anything and is counted apart.
    """
    if record.get("userSha256") == EMPTY_SHA256:
        return "unusable"
    if not any(record.get(name) for name in DIGESTS):
        raise AssertionError("a fidelity record carries no digest")
    messages = C.build_abstract_messages(record["items"], record["title"], record["journal"], note=record.get("note", ""))
    digests = C.digests(messages)
    for name in DIGESTS:
        if record.get(name) and record[name] != digests[name]:
            raise AssertionError(f"{name} differs for a recorded {record['journal']} call")
    return "checked"


class RecordCheckTests(unittest.TestCase):
    """The private-record check itself, on records built here."""

    def setUp(self):
        self.items = load("judge_items.json")["papers"][0]["items"][:3]
        self.messages = C.build_abstract_messages(self.items, REVIEW_TITLE, "Synthetic Journal of Examples")
        self.base = {"title": REVIEW_TITLE, "journal": "Synthetic Journal of Examples", "items": self.items}

    def test_message_list_digest_is_checked(self):
        record = {**self.base, "messagesSha256": hashlib.sha256(json.dumps(self.messages, ensure_ascii=False).encode()).hexdigest()}
        self.assertEqual("checked", check_record(record))
        with self.assertRaises(AssertionError):
            check_record({**record, "messagesSha256": "0" * 64})
        repair = C.build_repair_messages(self.items, REVIEW_TITLE, "Synthetic Journal of Examples")
        self.assertEqual("checked", check_record({**self.base, "note": C.repair_note(),
                                                  "messagesSha256": C.digests(repair)["messagesSha256"]}))

    def test_an_empty_recorded_prompt_is_unusable_not_a_pass(self):
        self.assertEqual("unusable", check_record({**self.base, "userSha256": EMPTY_SHA256, "systemSha256": "0" * 64}))
        with self.assertRaises(AssertionError):
            check_record(dict(self.base))


@unittest.skipUnless(os.environ.get("OCSL_REVIEW_CENSUS_FIDELITY_RECORDS"), "private fidelity records not configured")
class PrivateRecordTests(unittest.TestCase):
    """JSONL records {"title", "journal", "items", "note", and digests: see check_record} from a private fixture."""

    def test_private_records(self):
        path = Path(os.environ["OCSL_REVIEW_CENSUS_FIDELITY_RECORDS"])
        outcomes = {"checked": 0, "unusable": 0}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                outcomes[check_record(json.loads(line))] += 1
        self.assertGreater(outcomes["checked"], 0, outcomes)


if __name__ == "__main__":
    unittest.main()
