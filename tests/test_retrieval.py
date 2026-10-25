# SPDX-License-Identifier: Apache-2.0
"""FTS5/BM25 retrieval with receipts; synthetic evidence only."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ocsl_review.retrieval import (
    EvidenceIndex,
    EvidenceUnit,
    Work,
    WorkCatalog,
    fts5_available,
    load_library,
    load_store,
    query_terms,
)

ROOT = Path(__file__).resolve().parents[1]

UNITS = [
    EvidenceUnit("w1#abstract", "w1", "abstract", "Statin therapy lowered LDL cholesterol and reduced cardiovascular events in adults."),
    EvidenceUnit("w1#p1", "w1", "full-text", "The trial enrolled 4,000 participants with elevated cholesterol."),
    EvidenceUnit("w1#p2", "w1", "full-text", "Muscle symptoms were rare and did not differ between groups."),
    EvidenceUnit("w2#abstract", "w2", "abstract", "Metformin use was associated with lower cancer incidence in a cohort of diabetics."),
    EvidenceUnit("w2#p1", "w2", "full-text", "Cancer incidence was ascertained from national registries."),
    EvidenceUnit("w3#abstract", "w3", "abstract", "Exercise improved cognition in older adults; cholesterol was unchanged."),
    EvidenceUnit("w4#abstract", "w4", "abstract", "Die Studie untersuchte Müller-Zellen in der Netzhaut."),
]


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def build(self, name="index.sqlite3", *, use_fts5=None):
        return EvidenceIndex.build(self.root / name, UNITS, use_fts5=use_fts5)

    def test_query_terms_drop_stopwords_and_fold_diacritics(self):
        self.assertEqual(["statins", "lower", "ldl", "cholesterol", "muller", "il", "6"],
                         query_terms("Statins lower the LDL cholesterol of Müller and IL-6 (et al.)."))

    def test_cited_work_scope_and_store_scope(self):
        with self.build() as index:
            cited = index.search("Statins lower LDL cholesterol", work_ids=["w1"], k=2)
            self.assertEqual(["w1#abstract", "w1#p1"], [hit.unit_id for hit in cited.hits])
            self.assertTrue(all(hit.work_id == "w1" for hit in cited.hits))
            self.assertEqual({"kind": "works", "workIds": ["w1"]}, dict(cited.scope))
            store = index.search("cholesterol", k=5)
            self.assertEqual({"w1", "w3"}, {hit.work_id for hit in store.hits})
            self.assertEqual({"kind": "store"}, dict(store.scope))
            self.assertEqual([1, 2, 3], [hit.rank for hit in store.hits])
            self.assertEqual([], list(index.search("zebrafish", k=3).hits))
            self.assertEqual([], list(index.search("cholesterol", work_ids=[], k=3).hits))
            self.assertEqual(["w4#abstract"], [hit.unit_id for hit in index.search("Muller cells", k=3).hits])

    def test_receipts_are_content_addressed_and_replayable(self):
        with self.build() as index:
            receipt = index.search("Metformin and cancer incidence", k=3)
            record = receipt.record()
            self.assertEqual(index.digest, record["indexDigest"])
            self.assertEqual(hashlib.sha256("Metformin and cancer incidence".encode()).hexdigest(), record["querySha256"])
            self.assertEqual(receipt.receipt_id, index.search("Metformin and cancer incidence", k=3).receipt_id)
            self.assertTrue(index.replay(record)["reproduced"])
            tampered = json.loads(json.dumps(record))
            tampered["results"][0]["unitId"] = "w1#p2"
            self.assertFalse(index.replay(tampered)["reproduced"])
            self.assertEqual("index digest differs", index.replay({**record, "indexDigest": "0" * 64})["reason"])

    def test_index_digest_depends_only_on_content(self):
        with self.build("a.sqlite3") as first, EvidenceIndex.build(self.root / "b.sqlite3", list(reversed(UNITS))) as second:
            self.assertEqual(first.digest, second.digest)
        changed = UNITS[:-1] + [EvidenceUnit("w4#abstract", "w4", "abstract", "Changed text.")]
        with EvidenceIndex.build(self.root / "c.sqlite3", changed) as third:
            self.assertNotEqual(first.digest, third.digest)
        with self.assertRaises(ValueError):
            EvidenceIndex.build(self.root / "a.sqlite3", UNITS)
        with self.assertRaises(ValueError):
            EvidenceIndex.build(self.root / "d.sqlite3", UNITS + [UNITS[0]])

    @unittest.skipUnless(fts5_available(), "SQLite without FTS5")
    def test_python_fallback_ranks_like_fts5(self):
        with self.build("fts.sqlite3", use_fts5=True) as fts, self.build("py.sqlite3", use_fts5=False) as python:
            self.assertEqual("sqlite-fts5-bm25", fts.engine)
            self.assertEqual("python-bm25", python.engine)
            for query in ("cholesterol events adults", "cancer incidence registries", "muscle symptoms groups"):
                with self.subTest(query=query):
                    a, b = fts.search(query, k=5), python.search(query, k=5)
                    self.assertEqual([hit.unit_id for hit in a.hits], [hit.unit_id for hit in b.hits])
                    for left, right in zip(a.hits, b.hits):
                        self.assertAlmostEqual(left.score, right.score, places=4)

    def test_rerank_hook_is_recorded_and_cannot_add_hits(self):
        def reverse(query, hits):
            return list(reversed(hits))
        reverse.name = "reverse-test"
        with self.build() as index:
            plain = index.search("cholesterol", k=3)
            reranked = index.search("cholesterol", k=3, reranker=reverse)
            self.assertEqual([h.unit_id for h in reversed(plain.hits)], [h.unit_id for h in reranked.hits])
            self.assertEqual("reverse-test", reranked.record()["reranker"])
            self.assertNotEqual(plain.receipt_id, reranked.receipt_id)
            self.assertFalse(index.replay(reranked.record())["reproduced"])
            with self.assertRaises(ValueError):
                index.search("cholesterol", k=3, reranker=lambda q, hits: hits[:1])

    def test_work_catalog_identity_ladder(self):
        catalog = WorkCatalog([
            Work("w1", {"doi": "10.5555/Statin.1"}, "Statin therapy and cardiovascular events in adults"),
            Work("w2", {"pmid": "123456"}, "Metformin and cancer incidence"),
            Work("w3", {}, "Exercise and cognition in older adults: a randomised trial"),
            Work("w4", {}, "Cancer risk"),
        ])
        self.assertEqual(("w1", "resolved"), tuple(catalog.match({"doi": ["https://doi.org/10.5555/statin.1"]}, "x").get(k) for k in ("workId", "state")))
        self.assertEqual("w2", catalog.match({"pmid": ["123456"]}, "")["workId"])
        conflict = catalog.match({"doi": ["10.5555/statin.1"], "pmid": ["123456"]}, "")
        self.assertEqual(("conflict", ["w1", "w2"]), (conflict["state"], conflict["candidates"]))
        titled = catalog.match({}, "Doe, J. (2020). Exercise and cognition in older adults: a randomised trial. J, 1, 2.")
        self.assertEqual(("w3", "resolved_unverified", "title-containment"), (titled["workId"], titled["state"], titled["method"]))
        # Short titles never establish identity on their own.
        self.assertEqual("unresolved", catalog.match({}, "Doe, J. (2020). Cancer risk. J.")["state"])
        self.assertEqual("unresolved", catalog.match({}, "Doe, J. (2020). Unrelated work. J.")["state"])

    def test_store_and_library_loaders(self):
        store = self.root / "store.jsonl"
        store.write_text(json.dumps({"workId": "w9", "identifiers": {"doi": "10.5555/x"}, "title": "T", "year": "2020",
                                     "units": [{"kind": "abstract", "text": "An abstract."}]}) + "\n", encoding="utf-8")
        works = load_store(store)
        self.assertEqual(("w9", "w9#u1", 2020), (works[0].work_id, works[0].units[0].unit_id, works[0].year))
        database = self.root / "corpus.sqlite3"
        connection = sqlite3.connect(database)
        connection.executescript((ROOT / "skills/ocsl-review-reference-analysis/assets/literature-schema.sql").read_text())
        digest = "a" * 64
        text = "Statins reduced events."
        connection.execute("INSERT INTO works VALUES ('w1','Statin trial',2019,'{\"authors\":[\"Doe\"]}')")
        connection.execute("INSERT INTO source_artifacts VALUES (?,?,?,?,?,?)", (digest, "sources/" + digest, 1, "text/plain", "x", "t"))
        connection.execute("INSERT INTO work_identifiers VALUES ('w1','doi','10.5555/s',?)", (digest,))
        connection.execute("INSERT INTO source_sections VALUES ('s1','w1',?,'abstract','abstract',?,?,'x',1)",
                           (digest, text, hashlib.sha256(text.encode()).hexdigest()))
        connection.commit()
        connection.close()
        works = load_library(database)
        self.assertEqual(("w1", {"doi": "10.5555/s"}, "abstract", ["Doe"]),
                         (works[0].work_id, works[0].identifiers, works[0].units[0].kind, works[0].authors))


if __name__ == "__main__":
    unittest.main()
