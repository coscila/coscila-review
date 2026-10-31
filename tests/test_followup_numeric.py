# SPDX-License-Identifier: Apache-2.0
"""numeric-check@1 on S3 loci and the deterministic comparison numeric-comparison@1 with a synthetic table source."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

import followup_fixtures as F
from ocsl_review import followup_numeric, followup_tables, numeric_check
from ocsl_review.claim_check import ConfidentialPolicy
from ocsl_review.errors import ConfidentialityError, ValidationError
from ocsl_review.followup_model import WorkRef
from ocsl_review.followup_numeric import ClaimValue, normalize_value
from test_followup_strata import A1, A2, B1, frozen_branch, lid, protocol_with, synthetic_parent

TABLES = F.FIXTURES / "tables" / "synthetic-statistics-tables.json"
TABLES_SHA = hashlib.sha256(TABLES.read_bytes()).hexdigest()
JA, JB = F.JOURNAL_A, F.JOURNAL_B
REPORT = WorkRef("w-report", doi="10.5555/fu.work.report", title="Synthetic disease statistics report, second edition")
EXTRA_WORKS = [("w-report2", "10.5555/fu.work.report2", None, "Synthetic registry statistics report", "2019")]
EXTRA_CITATIONS = [
    (JA, A1, "5", "w-report", "true", None, [(8, "The disease caused 7.3 thousand deaths in Exampleland in 2019 ⟨R5⟩ .")]),
    (JA, A1, "6", "w-report", "true", None, [(9, "The disease caused 9.1 thousand deaths in Exampleland in 2019 ⟨R6⟩ .")]),
    (JA, A1, "7", "w-report", "true", None, [(10, "Reports of this kind guide screening policy ⟨R7⟩ .")]),
    (JB, B1, "5", "w-report2", "true", None, [(5, "Survival reached 64.5% in Exampleland in 2019 ⟨T5⟩ .")]),
    (JA, A2, "4", "w-report", "true", None,
     [(8, "Five-year survival was 64.5% in the Republic of Exampleland in 2019 ⟨S4⟩ .")]),
]


def parse(value_text, *, condition="all", metric="mortality-count", geography="Exampleland", year=2019, unit="deaths",
          quote=None, decision="parsed"):
    return {"id": "x", "decision": decision, "condition": condition, "metric": metric, "geography": geography,
            "year": year, "valueText": value_text, "unit": unit, "quote": quote or value_text,
            "rationale": "A statistic.", "abstainReason": ""}


# The scripted judge's parses, keyed by the marker of the cited work.
PARSES = {
    "⟨R3⟩": parse("12.5 thousand", condition="disease", quote="caused 12.5 thousand deaths"),
    "⟨R5⟩": parse("7.3 thousand", condition="disease", quote="caused 7.3 thousand deaths"),
    "⟨R6⟩": parse("9.1 thousand", condition="disease", quote="caused 9.1 thousand deaths"),
    "⟨R7⟩": {**parse("", decision="not-epidemiological"), "condition": "", "metric": "", "geography": "", "year": None,
             "unit": "", "quote": ""},
    "⟨T4⟩": parse("40", metric="incidence-rate", year=2018, unit="per population unit",
                  quote="reached 40 per population unit"),
    "⟨T5⟩": parse("64.5%", metric="survival", unit="%", quote="reached 64.5% in Exampleland"),
    "⟨S4⟩": parse("64.5%", metric="survival", geography="Republic of Exampleland", unit="%",
                  quote="survival was 64.5%"),
}


def answer(messages, settings):
    return json.dumps({"items": [{**PARSES[item["marker"]], "id": item["id"]} for item in F.items_of(messages)]})


def numeric_protocol(**s3):
    return protocol_with(S3={"licensingVerified": True,
                             "tableSource": {"declaredVersion": "synthetic-tables-1", "sha256": TABLES_SHA}, **s3})


class NumberTests(unittest.TestCase):
    def test_value_text_is_normalised_with_its_stated_precision(self):
        cases = {
            "12.5 thousand": ClaimValue(Decimal("12.5"), 1, 3), "12,500": ClaimValue(Decimal("12500"), 0),
            "12 500": ClaimValue(Decimal("12500"), 0), "12 500": ClaimValue(Decimal("12500"), 0),
            "3,4 %": ClaimValue(Decimal("3.4"), 1, 0, True), "1.234,5": ClaimValue(Decimal("1234.5"), 1),
            "1,234.5": ClaimValue(Decimal("1234.5"), 1), "0.125": ClaimValue(Decimal("0.125"), 3),
            "1.234.567": ClaimValue(Decimal("1234567"), 0), "1.5 billion": ClaimValue(Decimal("1.5"), 1, 9),
            "2 millions": ClaimValue(Decimal("2"), 0, 6), "64.5%": ClaimValue(Decimal("64.5"), 1, 0, True),
            "18 per cent": ClaimValue(Decimal("18"), 0, 0, True), "−2.5 percent": ClaimValue(Decimal("-2.5"), 1, 0, True),
            "second": ClaimValue(Decimal(2), 0), "3rd": ClaimValue(Decimal(3), 0), "about 40,": ClaimValue(Decimal(40), 0),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expected, normalize_value(text))
        for text in ("about forty", "", "1,23,4"):
            with self.subTest(text=text):
                self.assertIsNone(normalize_value(text))
        self.assertEqual(12500.0, normalize_value("12.5 thousand").value)

    def test_rounding_to_the_claimed_precision(self):
        claim = normalize_value("12.5 thousand")
        for value, expected in ((12500, True), (12460, True), (12549, True), (12551, False), (12449, False)):
            with self.subTest(value=value):
                self.assertEqual(expected, claim.matches(value, "deaths"))
        self.assertFalse(normalize_value("40").matches(38.0))
        self.assertTrue(normalize_value("40").matches(40.0))
        self.assertTrue(normalize_value("38").matches(38.4))
        self.assertTrue(normalize_value("12,500").matches(12.5, "thousand deaths"))
        self.assertFalse(normalize_value("64.5%").matches(64.5, "deaths"))
        self.assertTrue(normalize_value("64.5%").matches(64.5, "percent"))
        self.assertFalse(normalize_value("64.5").matches(64.5, "%"))
        self.assertTrue(normalize_value("1.5 billion").matches(1.46e9))


class ComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = followup_tables.load(TABLES)

    def classify(self, output, work=REPORT):
        return followup_numeric.compare(output, work, self.source)

    def test_every_comparison_class(self):
        consistent = self.classify(parse("12.5 thousand", condition="disease"))
        self.assertEqual(("consistent", 1, None, 12500.0),
                         (consistent["class"], consistent["assessable"], consistent["reason"], consistent["value"]))
        self.assertEqual([{"report": "synthetic-disease-statistics", "edition": "second", "condition": "all",
                           "metric": "mortality-count", "geography": "Exampleland", "year": 2019, "value": 12500,
                           "unit": "deaths", "locator": "table 1"}], consistent["matched"]["cells"])
        geography = self.classify(parse("7.3 thousand"))
        self.assertEqual("wrong-geography", geography["class"])
        self.assertEqual(["Otherland"], [cell["geography"] for cell in geography["matched"]["cells"]])
        edition = self.classify(parse("40", metric="incidence-rate", year=2018, unit="per population unit"))
        self.assertEqual("wrong-edition", edition["class"])
        self.assertEqual([("first", 2016)], [(cell["edition"], cell["year"]) for cell in edition["matched"]["cells"]])
        absent = self.classify(parse("9.1 thousand"))
        self.assertEqual("number-absent-or-different", absent["class"])
        self.assertEqual([12500], [cell["value"] for cell in absent["matched"]["cells"]])
        self.assertEqual("non-epidemiological", self.classify(parse("", decision="not-epidemiological"))["class"])
        self.assertEqual("consistent", self.classify(parse("64.5%", metric="survival", unit="%",
                                                           geography="Republic of Exampleland"))["class"])
        self.assertEqual("consistent", self.classify(parse("12.5 thousand", geography=""))["class"])
        self.assertEqual("number-absent-or-different", self.classify(parse("12.5 thousand", year=2018))["class"])

    def test_unassessable_comparisons_keep_their_reason(self):
        cases = {
            "abstained": (parse("", decision="abstain"), REPORT),
            "no-matching-table": (parse("12.5 thousand"), WorkRef("w-plain-1", doi="10.5555/fu.work.plain1")),
            "invalid-output": (parse("about forty"), REPORT),
        }
        for reason, (output, work) in cases.items():
            with self.subTest(reason=reason):
                result = self.classify(output, work)
                self.assertEqual((None, 0, reason, None), (result["class"], result["assessable"], result["reason"],
                                                           result["matched"]))
        with tempfile.TemporaryDirectory() as directory:
            data = json.loads(TABLES.read_text(encoding="utf-8"))
            data["reports"][0]["identifiers"]["doi"].append("10.5555/fu.work.report.any")
            path = Path(directory) / "tables.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            source = followup_tables.load(path)
            result = followup_numeric.compare(parse("12.5 thousand"), WorkRef("w", doi="10.5555/fu.work.report.any"),
                                              source)
        self.assertEqual("table-unavailable", result["reason"])

    def test_markers_are_found_in_their_sentences(self):
        marker = followup_numeric.marker_for
        self.assertEqual("⟨B12⟩", marker("Text ⟨B11⟩ ⟨B12⟩ .", "B12", "12", "jats"))
        self.assertEqual("⟨CR3⟩", marker("Text ⟨CR3⟩ ⟨CR4⟩ .", "ref-CR3", "3", "html"))
        self.assertEqual("⟨R3⟩", marker("Only one ⟨R3⟩ .", "ref-3", "3", "jats"))
        self.assertEqual("⟨R1⟩", marker("Two ⟨R1⟩ ⟨R2⟩ .", "ref-1", "1", "jats"))
        self.assertIsNone(marker("Two ⟨x⟩ ⟨y⟩ .", "ref-1", "1", "jats"))


class NumericCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.census = synthetic_parent(cls.root / "census", works=F.WORKS + EXTRA_WORKS,
                                      citations=F.CITATIONS + EXTRA_CITATIONS)
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def branch(self, protocol=None, *, text=False):
        type(self).count += 1
        return frozen_branch(self.root, f"numeric-{self.count}.sqlite3", protocol or numeric_protocol(),
                             census=self.census, text=text)

    def run_numeric(self, branch, judge=None, tables=TABLES, **options):
        options.setdefault("allow_synthetic", True)
        return followup_numeric.run_numeric(branch, tables, judge=judge or F.ScriptedJudge(answer),
                                            policy=ConfidentialPolicy("public-literature"), model=F.MODEL, **options)

    def rows(self, branch):
        with closing(branch.connect()) as connection:
            return {row["locus_id"]: dict(row) for row in connection.execute("SELECT * FROM fu_numeric")}

    def test_every_class_end_to_end_without_a_table_value_in_any_request(self):
        branch = self.branch(text=True)
        judge = F.ScriptedJudge(answer)
        result = self.run_numeric(branch, judge)
        rows = self.rows(branch)
        expected = {lid(JA, A1, "3"): ("consistent", None), lid(JA, A1, "5"): ("wrong-geography", None),
                    lid(JA, A1, "6"): ("number-absent-or-different", None),
                    lid(JA, A1, "7"): ("non-epidemiological", None), lid(JB, B1, "4"): ("wrong-edition", None),
                    lid(JB, B1, "5"): (None, "no-matching-table"), lid(JA, A2, "4"): ("consistent", None)}
        self.assertEqual(expected, {key: (row["class"], row["reason"]) for key, row in rows.items()})
        consistent = rows[lid(JA, A1, "3")]
        self.assertEqual((1, "disease", "mortality-count", "Exampleland", 2019, 12500.0, "12.5 thousand",
                          followup_numeric.RULE), (consistent["epidemiological"], consistent["condition"],
                                                   consistent["metric"], consistent["geography"], consistent["year"],
                                                   consistent["value"], consistent["value_text"],
                                                   consistent["rule_version"]))
        self.assertEqual(result["tableSource"], consistent["table_source"])
        self.assertEqual(["table 1"], [cell["locator"] for cell in json.loads(consistent["matched_json"])["cells"]])
        self.assertEqual(0, rows[lid(JA, A1, "7")]["epidemiological"])
        self.assertIsNone(rows[lid(JB, B1, "5")]["matched_json"])
        self.assertEqual((7, 7, 0), (result["units"], result["judging"]["accepted"], result["judging"]["rejected"]))
        for messages, _ in judge.requests:
            body = messages[1]["content"]
            for item in json.loads(body)["items"]:
                self.assertEqual({"id", "marker", "citingSentence", "citedWork"}, set(item))
                self.assertEqual({"title", "year"}, set(item["citedWork"]))
            for table_value in ("7300", "38.0", "12500", "40.0", "Otherland", "table 1"):
                self.assertNotIn(table_value, body)
        with closing(branch.connect()) as connection:
            dataset = connection.execute("SELECT kind, sha256 FROM fu_datasets WHERE dataset_id=?",
                                         (result["tableSource"],)).fetchone()
        self.assertEqual(("table-source", TABLES_SHA), tuple(dataset))
        self.assertTrue(branch.verify()["ok"])

    def test_redacted_sentences_are_never_sent_and_entitled_ones_only_on_premises(self):
        branch = self.branch()
        judge = F.ScriptedJudge(answer)
        result = self.run_numeric(branch, judge)
        rows = self.rows(branch)
        self.assertEqual((0, "text-redacted"), (rows[lid(JA, A2, "4")]["assessable"], rows[lid(JA, A2, "4")]["reason"]))
        self.assertEqual((1, 6), (result["blocked"], result["sent"]))
        sent = [item["id"] for messages, _ in judge.requests for item in F.items_of(messages)]
        self.assertNotIn(lid(JA, A2, "4"), sent)
        self.assertEqual(0, self.run_numeric(branch, judge)["newlyBlocked"])
        external = F.ScriptedJudge(answer, location="external")
        text_branch = self.branch(text=True)
        with self.assertRaisesRegex(ConfidentialityError, "on premises"):
            self.run_numeric(text_branch, external)
        self.assertEqual([], external.requests)
        public = self.run_numeric(text_branch, external, public_only=True)
        self.assertEqual(6, public["sent"])
        self.assertEqual((None, "not-judged"), (self.rows(text_branch)[lid(JA, A2, "4")]["class"],
                                                self.rows(text_branch)[lid(JA, A2, "4")]["reason"]))

    def test_licensing_and_the_protocols_pin_are_enforced(self):
        branch = self.branch()
        with self.assertRaisesRegex(ValidationError, "allow-synthetic-tables"):
            self.run_numeric(branch, allow_synthetic=False)
        for name, change, message in (
                ("unverified", lambda d: d.update(licensing="unverified"), "unverified"),
                ("changed", lambda d: d["reports"][0]["editions"]["second"]["cells"][0].update(value=12600), "sha256"),
                ("verified", lambda d: d.update(licensing="verified"), "sha256")):
            data = json.loads(TABLES.read_text(encoding="utf-8"))
            change(data)
            path = self.root / f"tables-{name}.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.subTest(source=name):
                with self.assertRaisesRegex(ValidationError, message):
                    self.run_numeric(branch, tables=path)
        protocol = numeric_protocol()
        protocol["strata"]["S3"]["tableSource"]["declaredVersion"] = "another-version"
        with self.assertRaisesRegex(ValidationError, "declared version"):
            self.run_numeric(self.branch(protocol))
        unpinned = numeric_protocol()
        del unpinned["strata"]["S3"]["tableSource"]
        with self.assertRaisesRegex(ValidationError, "pins no table source"):
            self.run_numeric(self.branch(unpinned))
        with self.assertRaisesRegex(ValidationError, "drops S3"):
            self.run_numeric(self.branch(protocol_with()))
        with closing(branch.connect()) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM fu_numeric").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM fu_datasets WHERE kind='table-source'")
                             .fetchone()[0])

    def test_judging_needs_a_freeze_and_the_pinned_model(self):
        branch = self.branch()
        with self.assertRaisesRegex(ValidationError, "model.served"):
            followup_numeric.run_numeric(branch, TABLES, judge=F.ScriptedJudge(answer), policy=None,
                                         model="another-model", allow_synthetic=True)
        from test_followup_strata import insert_statuses, make_branch
        unfrozen = make_branch(self.root, "numeric-unfrozen.sqlite3", numeric_protocol(), census=self.census)
        insert_statuses(unfrozen)
        with self.assertRaisesRegex(ValidationError, "not frozen"):
            self.run_numeric(unfrozen)

    def test_reruns_resume_rejections_become_invalid_outputs_and_replicates_leave_rows(self):
        branch = self.branch(text=True)

        def bad(messages, settings):
            items = []
            for item in F.items_of(messages):
                output = {**PARSES[item["marker"]], "id": item["id"]}
                if item["marker"] == "⟨R5⟩":
                    output["valueText"] = "7300"
                items.append(output)
            return json.dumps({"items": items})
        first = self.run_numeric(branch, F.ScriptedJudge(bad))
        # rejected in the first call and again in its repair call
        self.assertEqual((6, 2, 1), (first["judging"]["accepted"], first["judging"]["rejected"],
                                     first["judging"]["repairCalls"]))
        self.assertEqual((None, "invalid-output"), (self.rows(branch)[lid(JA, A1, "5")]["class"],
                                                    self.rows(branch)[lid(JA, A1, "5")]["reason"]))
        again = self.run_numeric(branch, F.ScriptedJudge(answer))
        self.assertEqual((6, 1), (again["judging"]["skipped"], again["judging"]["accepted"]))
        self.assertEqual("wrong-geography", self.rows(branch)[lid(JA, A1, "5")]["class"])
        before = self.rows(branch)
        judge = F.ScriptedJudge(answer)
        replicate = self.run_numeric(branch, judge, variant="replicate")
        self.assertIsNone(replicate["rows"])
        self.assertEqual(4, replicate["sent"])
        self.assertEqual(before, self.rows(branch))
        limited_branch = self.branch(text=True)
        limited = self.run_numeric(limited_branch, F.ScriptedJudge(answer), limit=2)
        self.assertEqual(2, limited["sent"])
        self.assertEqual(5, [row["reason"] for row in self.rows(limited_branch).values()].count("not-judged"))

    def test_the_command_line_runs_numeric_with_the_judge_options(self):
        branch = self.branch(text=True)
        from ocsl_review.cli import main
        target = self.root / "numeric-result.json"
        judge = F.ScriptedJudge(answer)
        with mock.patch("ocsl_review.followup_numeric.judge_from_args",
                        return_value=(judge, ConfidentialPolicy("public-literature"))) as built:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = main(["--result-file", str(target), "followup", "numeric", "--database", str(branch.path),
                             "--tables", str(TABLES), "--allow-synthetic-tables", "--model", F.MODEL,
                             "--base-url", "http://judge.example/v1", "--on-premises", "--transport-retries", "0"])
        record = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(0, code, record)
        self.assertEqual(7, record["result"]["judging"]["accepted"])
        self.assertTrue(built.call_args.args[0].allow_synthetic_tables)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["--result-file", str(target), "followup", "numeric", "--database", str(branch.path),
                         "--tables", str(TABLES), "--model", F.MODEL, "--base-url", "http://judge.example/v1"])
        record = json.loads(target.read_text(encoding="utf-8"))
        self.assertNotEqual(0, code)
        self.assertEqual("validation", record["error"]["kind"])


class InstrumentRuleTests(unittest.TestCase):
    def test_parses_must_be_written_in_the_sentence(self):
        item = {"id": "locus-3", "marker": "⟨R3⟩", "provenance": "public",
                "citingSentence": "The disease caused 12.5 thousand deaths in Exampleland in 2019 ⟨R3⟩ .",
                "citedWork": {"title": "Synthetic disease statistics report, second edition", "year": 2020}}
        good = {**PARSES["⟨R3⟩"], "id": "locus-3"}
        self.assertEqual(["locus-3"], list(numeric_check.validate_response([item], json.dumps({"items": [good]}))
                                           ["accepted"]))
        for field, value in (("geography", "Otherland"), ("year", 2018), ("condition", "breast cancer")):
            output = copy.deepcopy(good)
            output[field] = value
            with self.subTest(field=field):
                result = numeric_check.validate_response([item], json.dumps({"items": [output]}))
                self.assertEqual("invalid_parse", result["rejected"]["locus-3"]["code"])
        self.assertEqual({"epidemiological": 1, "condition": "disease", "metric": "mortality-count",
                          "geography": "Exampleland", "value_text": "12.5 thousand", "unit": "deaths",
                          "quote": "caused 12.5 thousand deaths", "year": 2019}, numeric_check.parse_fields(good))
        self.assertEqual(0, numeric_check.parse_fields(PARSES["⟨R7⟩"])["epidemiological"])
        self.assertIsNone(numeric_check.parse_fields({**PARSES["⟨R7⟩"], "decision": "abstain"})["epidemiological"])


if __name__ == "__main__":
    unittest.main()
