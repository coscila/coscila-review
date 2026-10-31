# SPDX-License-Identifier: Apache-2.0
"""citation-function@1 runs: what the judge sees (never status, stratum, verdicts, identifiers or journal), markers
of the named work, neighbouring sentences from a synthetic JATS review, entitled routing, abstentions, invalid
outputs, blocked units and resumption."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import followup_fixtures as F
from ocsl_review import followup_function as FN
from ocsl_review import instruments, scores_db
from ocsl_review.cli import main
from ocsl_review.errors import ConfidentialityError, ValidationError
from ocsl_review.followup_branch import FollowupBranch

from test_followup_dependency import insert_frame, locus


def ref(rid: str, number: int) -> str:
    return f'<ref id="{rid}"><label>{number}</label><mixed-citation>Synthetic reference {number}.</mixed-citation></ref>'


# Review a1 as the census extracted it: three citing units before the shared sentence (unit 3), which has a
# neighbour on each side.
REVIEW_A1 = ('<article><front><article-meta><article-id pub-id-type="doi">10.5555/fu.review.a1</article-id>'
             '<title-group><article-title>Synthetic review 1</article-title></title-group></article-meta></front>'
             '<body><sec><title>Results</title>'
             '<p>Background one <xref ref-type="bibr" rid="R5">5</xref>.</p>'
             '<p>Background two <xref ref-type="bibr" rid="R5">5</xref>.</p>'
             '<p>Background three <xref ref-type="bibr" rid="R5">5</xref>.</p>'
             '<p>Earlier cohorts were small and heterogeneous. Marker zeta doubles the risk in adults '
             '<xref ref-type="bibr" rid="R1">1</xref><xref ref-type="bibr" rid="R2">2</xref>. '
             'The association was later disputed after a retraction.</p>'
             '</sec></body><back><ref-list>' + ref("R1", 1) + ref("R2", 2) + ref("R5", 5) +
             '</ref-list></back></article>').encode()
DOCUMENT_SHA = hashlib.sha256(REVIEW_A1).hexdigest()


def make_parent_with_document(root: Path) -> dict[str, Path]:
    working = F.make_working_census(root / "census-working.sqlite3")
    with closing(scores_db.connect(working)) as connection:
        connection.execute("UPDATE reviews SET document_sha256=? WHERE doi='10.5555/fu.review.a1'", (DOCUMENT_SHA,))
        connection.commit()
    parent, text = root / "census-frozen.sqlite3", root / "census-frozen-text.sqlite3"
    scores_db.freeze(working, parent)
    scores_db.freeze(working, text, keep_text=True)
    documents = root / "documents"
    documents.mkdir()
    (documents / f"{DOCUMENT_SHA}.xml").write_bytes(REVIEW_A1)
    return {"parent": parent, "text": text, "documents": documents}


def quote_of(item):
    return item["citingSentence"].split(" ⟨")[0]


def relies(messages, settings):
    return json.dumps({"items": [{"id": item["id"], "function": "relies", "quote": quote_of(item),
                                  "quoteField": "citingSentence", "rationale": "The review rests a statement on it.",
                                  "abstainReason": ""} for item in F.items_of(messages)]})


class FunctionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.census = make_parent_with_document(cls.root)
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def branch(self, *, text=False, pilot=()):
        type(self).count += 1
        path = self.root / f"branch-{self.count}.sqlite3"
        FollowupBranch.create(path, self.census["parent"], parent_text=self.census["text"] if text else None,
                              protocol=F.protocol())
        branch = FollowupBranch(path)
        with branch.writing() as connection:
            insert_frame(connection, pilot=pilot)
        return branch

    def run_function(self, branch, judge, **options):
        options.setdefault("documents", FN.ReviewDocuments(self.census["documents"]))
        with branch.writing() as connection:
            return FN.run_function(connection, branch, judge, None, model=F.MODEL, **options)

    def rows(self, branch):
        with closing(branch.connect()) as connection:
            return {row["locus_id"]: dict(row) for row in connection.execute("SELECT * FROM fu_function")}

    def test_the_judge_sees_the_declared_fields_and_never_status_or_strata(self):
        branch = self.branch(text=True)
        judge = F.ScriptedJudge(relies)
        result = self.run_function(branch, judge)
        self.assertEqual(7, result["sent"])
        sent = [item for messages, _ in judge.requests for item in F.items_of(messages)]
        self.assertEqual(7, len(sent))
        for messages, _ in judge.requests:
            content = messages[1]["content"]
            for hidden in ("10.5555/fu.work", "10.5555/fu.review", F.JOURNAL_A, F.JOURNAL_B, "stratum", "co-cited",
                           "w-retracted", "provenance", "direct-support", "status"):
                self.assertNotIn(hidden, content)
        for item in sent:
            self.assertEqual({"id", "marker", "citingSentence", "contextBefore", "contextAfter", "section",
                              "citedWork"}, set(item))
            self.assertEqual({"title", "year"}, set(item["citedWork"]))
            self.assertIn(item["marker"], item["citingSentence"])
        by_id = {item["id"]: item for item in sent}
        self.assertEqual(("⟨R1⟩", "Earlier cohorts were small and heterogeneous.",
                          "The association was later disputed after a retraction."),
                         (by_id[locus("a1/1")]["marker"], by_id[locus("a1/1")]["contextBefore"],
                          by_id[locus("a1/1")]["contextAfter"]))
        self.assertEqual(("⟨T2⟩", "", ""), (by_id[locus("b1/2")]["marker"], by_id[locus("b1/2")]["contextBefore"],
                                            by_id[locus("b1/2")]["contextAfter"]))
        self.assertEqual({"bibliography-order": 2, "single-marker": 5}, result["markers"])
        self.assertEqual({"document": 1, "sentence-not-found": 1, "no-document": 5}, result["contexts"])
        rows = self.rows(branch)
        self.assertEqual(7, len(rows))
        self.assertEqual({"relies"}, {row["class"] for row in rows.values()})
        self.assertNotIn(locus("a1/2"), rows, "co-cited loci are not classified")

    def test_entitled_items_reach_only_on_premises_judges(self):
        branch = self.branch(text=True)
        external = F.ScriptedJudge(relies, location="external")
        with self.assertRaisesRegex(ConfidentialityError, "on premises"):
            self.run_function(branch, external)
        self.assertEqual([], external.requests)
        result = self.run_function(branch, external, public_only=True)
        self.assertEqual((4, 3), (result["sent"], result["withheldEntitled"]))
        rows = self.rows(branch)
        self.assertEqual("not-judged", rows[locus("a2/1")]["reason"])
        self.assertEqual("relies", rows[locus("b1/1")]["class"])

    def test_redacted_sentences_without_a_text_parent_are_blocked_unsent(self):
        branch = self.branch()
        judge = F.ScriptedJudge(relies)
        result = self.run_function(branch, judge)
        self.assertEqual((4, {"text-redacted": 3}), (result["sent"], result["blocked"]))
        again = self.run_function(branch, judge)
        self.assertEqual((0, 0), (again["sent"], again["blockedRecorded"]), "a second run resends and blocks nothing")
        rows = self.rows(branch)
        self.assertEqual({"text-redacted"}, {rows[locus(name)]["reason"] for name in ("a2/1", "a2/2", "a2/3")})
        with closing(branch.connect()) as connection:
            blocked = connection.execute("SELECT COUNT(*) FROM fu_judgments WHERE outcome='blocked'").fetchone()[0]
        self.assertEqual(3, blocked)

    def test_abstentions_invalid_outputs_and_units_not_yet_judged_keep_their_reasons(self):
        branch = self.branch(text=True)
        abstain, invalid = locus("a1/1"), locus("a2/2")

        def answer(messages, settings):
            outputs = []
            for item in F.items_of(messages):
                if item["id"] == abstain:
                    outputs.append({"id": item["id"], "function": "abstain", "quote": "", "quoteField": "",
                                    "rationale": "The sentence does not show how the work is used.",
                                    "abstainReason": "unclear use"})
                elif item["id"] == invalid:
                    outputs.append({"id": item["id"], "function": "critical", "quote": "a quote that is not there",
                                    "quoteField": "citingSentence", "rationale": "Disputed.", "abstainReason": ""})
                else:
                    outputs.append(json.loads(relies([messages[0], {"content": json.dumps({"items": [item]})}],
                                                     settings))["items"][0])
            return json.dumps({"items": outputs})
        result = self.run_function(branch, F.ScriptedJudge(answer), limit=5)
        self.assertEqual(5, result["sent"])
        self.assertEqual(1, result["run"]["repairCalls"])
        rows = self.rows(branch)
        self.assertEqual((0, "abstained", None), (rows[abstain]["assessable"], rows[abstain]["reason"],
                                                  rows[abstain]["class"]))
        self.assertEqual("invalid-output", rows[invalid]["reason"])
        self.assertEqual(2, sum(1 for row in rows.values() if row["reason"] == "not-judged"))
        self.assertEqual("citingSentence", next(row["quote_field"] for row in rows.values() if row["class"]))

    def test_pilot_scope_and_the_seeded_replicate(self):
        branch = self.branch(text=True, pilot=("a1/1", "a1/4"))
        judge = F.ScriptedJudge(relies)
        result = self.run_function(branch, judge, scope="pilot")
        self.assertEqual((2, 2), (result["selection"]["pilotUnits"], result["sent"]))
        replicate = self.run_function(branch, judge, variant="replicate")
        drawn = [name for name in ("a1/1", "a2/1", "a2/2", "b1/1", "b1/2", "a1/4", "a2/3")
                 if FN.replicate_draw(F.PROTOCOL["seed"], locus(name)) < 0.5]
        self.assertEqual(len(drawn), replicate["selection"]["replicateUnits"])
        self.assertEqual(FN.REPLICATE_RULE, replicate["selection"]["replicateRule"])
        with closing(branch.connect()) as connection:
            variants = dict(connection.execute("SELECT variant, COUNT(*) FROM fu_judgments WHERE outcome='accepted' "
                                               "GROUP BY variant").fetchall())
        self.assertEqual({"primary": 2, "replicate": len(drawn)}, variants)
        self.assertEqual(2, sum(1 for row in self.rows(branch).values() if row["class"] == "relies"),
                         "replicate verdicts never replace the primary rows")

    def test_markers_of_the_named_work(self):
        self.assertEqual(("⟨B7⟩", "target"), FN.resolve_marker("Text ⟨B3⟩ ⟨B7⟩ .", "B7", [(1, "B3"), (2, "B7")]))
        self.assertEqual(("⟨CR7⟩", "target"), FN.resolve_marker("Text ⟨CR3⟩ ⟨CR7⟩ .", "ref-CR7", []))
        self.assertEqual(("⟨x⟩", "single-marker"), FN.resolve_marker("Text ⟨x⟩ .", "ref-9", []))
        self.assertEqual((None, "unresolved"), FN.resolve_marker("Text ⟨a⟩ ⟨b⟩ ⟨c⟩ .", "ref-2",
                                                                 [(1, "ref-1"), (2, "ref-2")]))
        self.assertEqual(("⟨b⟩", "bibliography-order"),
                         FN.resolve_marker("Text ⟨a⟩ ⟨b⟩ .", "ref-2", [(1, "ref-1"), (2, "ref-2")]))

    def test_the_judge_must_serve_the_protocols_model(self):
        branch = self.branch()
        with branch.writing() as connection:
            with self.assertRaisesRegex(ValidationError, "differs from the served model"):
                FN.run_function(connection, branch, F.ScriptedJudge(relies), None, model="another-model")

    def test_the_command_writes_a_result_file(self):
        branch = self.branch(text=True)
        judge = F.ScriptedJudge(relies)
        target = self.root / f"result-function-{self.count}.json"
        with mock.patch("ocsl_review.followup_judge.judge_from_args", lambda args: (judge, None)), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["--result-file", str(target), "followup", "function", "--database", str(branch.path),
                         "--model", F.MODEL, "--documents", str(self.census["documents"]), "--concurrency", "1"])
        record = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(0, code, record)
        self.assertEqual((7, instruments.load("citation-function@1").digest),
                         (record["result"]["sent"], record["result"]["digest"]))


if __name__ == "__main__":
    unittest.main()
