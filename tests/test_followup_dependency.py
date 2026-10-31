# SPDX-License-Identifier: Apache-2.0
"""dependency-aggregation@1 on the synthetic census: sole, redundant, essential and undetermined anchors, full-text
verdicts only, unresolved co-citations, a work cited twice, and idempotent recomputation.

``insert_frame`` writes the rows the analysis track's strata builder owns (frame, strata, co-cited loci and a
protocol freeze) straight from the parent census, so the evidence tests never import that track's modules.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import followup_fixtures as F
from ocsl_review import followup_dependency as D
from ocsl_review.cli import main
from ocsl_review.followup_branch import FollowupBranch
from ocsl_review.followup_model import claim_id, locus_id, sentence_digest

REVIEWS = {"a1": (F.JOURNAL_A, "10.5555/fu.review.a1"), "a2": (F.JOURNAL_A, "10.5555/fu.review.a2"),
           "b1": (F.JOURNAL_B, "10.5555/fu.review.b1")}
# S1: loci citing the retracted, concern-flagged and corrected works; co-cited: the other work of the two shared
# citing sentences (review a1 and review b1 cite w-retracted together with w-plain-1); S0: two ordinary loci.
STRATA = {"S1": ["a1/1", "a2/1", "a2/2", "b1/1", "b1/2"], "S0": ["a1/4", "a2/3"], "co-cited": ["a1/2", "b1/3"]}


def locus(name: str) -> str:
    review, label = name.split("/")
    journal, doi = REVIEWS[review]
    return locus_id(journal, doi, label, 1)


def insert_frame(connection, strata=None, *, pilot=(), freeze=True) -> dict[str, str]:
    """Frame rows for every parent locus, the given strata (census selection), co-cited loci and a freeze."""
    rows = connection.execute(
        "SELECT l.journal, l.doi, l.label, l.seq, l.sentence, l.section, l.unit, c.cited_work_key, c.identity, "
        "c.cited_inbody, c.abs_category, c.ft_category, r.year, r.first_online, r.topic, r.stratum, "
        "r.provenance_class FROM census.loci l JOIN census.citations c ON c.journal=l.journal AND c.doi=l.doi AND "
        "c.label=l.label JOIN census.reviews r ON r.journal=l.journal AND r.doi=l.doi").fetchall()
    ids = {}
    for row in rows:
        digest = sentence_digest(row["sentence"])
        identifier = locus_id(row["journal"], row["doi"], row["label"], row["seq"])
        ids[identifier] = claim_id(row["journal"], row["doi"], row["unit"], digest)
        connection.execute("INSERT INTO fu_frame VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            identifier, row["journal"], row["doi"], row["label"], row["seq"], row["cited_work_key"], ids[identifier],
            digest, row["unit"], row["section"], row["year"], row["first_online"], row["topic"], row["stratum"],
            row["provenance_class"], row["identity"], row["cited_inbody"], row["abs_category"], row["ft_category"]))
    strata = STRATA if strata is None else strata
    pilots = {locus(name) for name in pilot}
    for stratum, names in strata.items():
        for name in names:
            identifier = locus(name)
            connection.execute("INSERT INTO fu_strata VALUES(?,?,1.0,'census',NULL,?,?)",
                               (stratum, identifier, int(identifier in pilots), 1.0 if identifier in pilots else None))
    for stratum in ("S1", "S2"):
        for name in strata.get(stratum, []):
            anchor = locus(name)
            for other, claim in ids.items():
                if other != anchor and claim == ids[anchor]:
                    connection.execute("INSERT OR IGNORE INTO fu_cocited VALUES(?,?,?)", (anchor, other, claim))
    if freeze:
        connection.execute("INSERT INTO fu_protocol_freeze VALUES('freeze-synthetic', ?, ?, 'status-synthetic', ?, "
                           "'[]', '{}', '2026-01-01T00:00:00.000000Z')", ("a" * 64, "b" * 64, "c" * 64))
    return ids


def support(connection, name, tier, verdict=None, reason=None):
    connection.execute("INSERT OR REPLACE INTO fu_support(locus_id, tier, verdict, assessable, reason) "
                       "VALUES(?,?,?,?,?)", (locus(name), tier, verdict, int(reason is None), reason))


class DependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.census = F.make_parent(cls.root)
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        type(self).count += 1
        self.path = self.root / f"branch-{self.count}.sqlite3"
        FollowupBranch.create(self.path, self.census["parent"], protocol=F.protocol())
        self.branch = FollowupBranch(self.path)

    def classes(self, connection):
        D.compute_dependency(connection)
        return {name: connection.execute("SELECT class FROM fu_dependency WHERE locus_id=?",
                                         (locus(name),)).fetchone()[0] for name in STRATA["S1"]}

    def test_every_branch_of_the_rule_on_the_fixture_census(self):
        with self.branch.writing() as connection:
            insert_frame(connection)
            self.assertEqual({"a1/1": "undetermined", "a2/1": "sole", "a2/2": "sole", "b1/1": "sole",
                              "b1/2": "undetermined"}, self.classes(connection))
            support(connection, "a1/2", "full-text", "direct-support")
            support(connection, "b1/3", "full-text", "contradicted")
            support(connection, "a1/1", "full-text", "partial-indirect")
            self.assertEqual({"a1/1": "redundant", "a2/1": "sole", "a2/2": "sole", "b1/1": "sole",
                              "b1/2": "essential"}, self.classes(connection))
            row = connection.execute("SELECT * FROM fu_dependency WHERE locus_id=?", (locus("a1/1"),)).fetchone()
            self.assertEqual((1, 1, 0, "partial-indirect", "dependency-aggregation@1"),
                             (row["cocited"], row["cocited_supporting"], row["cocited_unassessable"],
                              row["anchor_verdict"], row["rule_version"]))
            detail = json.loads(row["detail_json"])
            self.assertEqual(["w-plain-1"], [work["workKey"] for work in detail["cocitedWorks"]])
            self.assertEqual("direct-support", detail["cocitedWorks"][0]["loci"][0]["verdict"])
            # A failed download or abstract silence is never "not supporting".
            support(connection, "b1/3", "full-text", reason="fetch-failed")
            support(connection, "b1/3", "abstract", "contradicted")
            self.assertEqual("undetermined", self.classes(connection)["b1/2"])

    def test_recomputation_is_idempotent(self):
        with self.branch.writing() as connection:
            insert_frame(connection)
            support(connection, "a1/2", "full-text", "scope-caveat")
            first = D.compute_dependency(connection)
            rows = connection.execute("SELECT * FROM fu_dependency ORDER BY locus_id").fetchall()
            second = D.compute_dependency(connection)
            again = connection.execute("SELECT * FROM fu_dependency ORDER BY locus_id").fetchall()
        self.assertEqual(first, second)
        self.assertEqual([tuple(row) for row in rows], [tuple(row) for row in again])
        self.assertEqual({"redundant": 1, "sole": 3, "undetermined": 1}, first["classes"])

    def test_unresolved_co_citations_and_works_cited_twice(self):
        anchor = {"locus_id": "a", "claim_id": "c", "work_key": "w"}
        loci = [{"locusId": "a", "label": "1", "seq": 1, "workKey": "w", "identity": "resolved"},
                {"locusId": "b", "label": "2", "seq": 1, "workKey": "x", "identity": "resolved"},
                {"locusId": "b2", "label": "2", "seq": 2, "workKey": "x", "identity": "resolved"},
                {"locusId": "c", "label": "3", "seq": 1, "workKey": "w", "identity": "resolved"}]
        verdicts = {"b": ("contradicted", 1, None), "b2": (None, 0, "fetch-failed")}
        row = D.classify(anchor, loci, verdicts)
        self.assertEqual(("essential", 1, 0, 0), (row["class"], row["cocited"], row["cocited_supporting"],
                                                  row["cocited_unassessable"]))
        unresolved = loci + [{"locusId": "d", "label": "4", "seq": 1, "workKey": None, "identity": "unresolved"}]
        row = D.classify(anchor, unresolved, verdicts)
        self.assertEqual(("undetermined", 2, 1), (row["class"], row["cocited"], row["cocited_unassessable"]))
        row = D.classify(anchor, unresolved, {**verdicts, "b2": ("direct-support", 1, None)})
        self.assertEqual("redundant", row["class"])

    def test_the_command_needs_the_freeze(self):
        def invoke(name):
            target = self.root / f"result-{self.count}-{name}.json"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(["--result-file", str(target), "followup", "dependency", "--database", str(self.path)])
            return code, json.loads(target.read_text(encoding="utf-8"))
        code, record = invoke("unfrozen")
        self.assertEqual(("validation", 5), (record["error"]["kind"], code))
        with self.branch.writing() as connection:
            insert_frame(connection)
        code, record = invoke("frozen")
        self.assertEqual(0, code, record)
        self.assertEqual((5, "freeze-synthetic"), (record["result"]["anchors"], record["result"]["freeze"]))


if __name__ == "__main__":
    unittest.main()
