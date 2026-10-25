# SPDX-License-Identifier: Apache-2.0
"""Reference integrity classes with offline provider fakes; synthetic records only."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ocsl_review.confidentiality import EgressPlan
from ocsl_review.ingest import ingest_manuscript
from ocsl_review.integrity import (
    IntegrityChecker,
    RegistryCheck,
    RetractionWatchDataset,
    combine_status,
    crossref_notices,
    local_findings,
    metadata_findings,
    printed_volume_page,
    title_coverage,
)
from ocsl_review.scholarly import ScholarlyClient

from claim_check_fixtures import FakeRegistry, OfflineScholarlyTransport, crossref_message, retraction_watch_csv


class LocalClassTests(unittest.TestCase):
    def test_reconciliation_classes_and_identity_collisions(self):
        markdown = """# Introduction

Known (Smith, 2019; Doe, 2011) and also ambiguous (Lee, 2020). Numbered too [9].

## References

- Smith, J. (2019). First. J, 1, 1.
- Lee, K., & Park, D. (2020a). Second. J, 1, 2.
- Lee, K., & Ng, D. (2020b). Third. J, 1, 3.
- Never, C. (2001). Never cited. J, 1, 4.
- Alpha, B. (2002). Same work. J.
- Beta, B. (2002). Same work. J.
- Gamma, B. (2002). Same work. J.
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.md"
            path.write_text(markdown, encoding="utf-8")
            manuscript = ingest_manuscript(path)
        identities = {"ref-00001": {"workId": "w1"}, "ref-00002": {"workId": "w2"}, "ref-00003": {"workId": "w2"},
                      "ref-00004": {"workId": None, "state": "unresolved"}, "ref-00005": {"workId": "w5"},
                      "ref-00006": {"workId": "w5"}, "ref-00007": {"workId": "w5"}}
        findings = local_findings(manuscript, identities)
        classes = sorted(item["class"] for item in findings)
        self.assertEqual(["ambiguous-citation", "cited-not-listed", "cited-not-listed", "duplicate-identity",
                          "identity-unresolved", "listed-not-cited", "listed-not-cited", "listed-not-cited",
                          "listed-not-cited", "listed-not-cited", "listed-not-cited", "repeated-entry"], classes)
        by_class = {}
        for item in findings:
            by_class.setdefault(item["class"], []).append(item)
        self.assertEqual(["ref-00005", "ref-00006", "ref-00007"], by_class["duplicate-identity"][0]["detail"]["entries"])
        self.assertEqual("info", by_class["repeated-entry"][0]["severity"])
        self.assertEqual({"Doe 2011", "9"}, {item["detail"].get("label") or f"{item['detail']['authors'][0]} {item['detail']['year']}"
                                             for item in by_class["cited-not-listed"]})
        self.assertEqual(["ref-00002", "ref-00003"], by_class["ambiguous-citation"][0]["detail"]["candidates"])
        self.assertEqual("support verdicts for this entry stay unsure", by_class["identity-unresolved"][0]["detail"]["consequence"])
        # Stable ids: the same manuscript yields the same finding ids.
        self.assertEqual([item["findingId"] for item in findings],
                         [item["findingId"] for item in local_findings(manuscript, identities)])


class RegistryTests(unittest.TestCase):
    def test_three_independent_refusals_are_needed(self):
        registry = FakeRegistry({"10.5555/gone": (404, 404, 404), "10.5555/odd": (404, 302, 404),
                                 "10.5555/blocked": (403, 429, 404), "10.5555/<old>;x#1": (404, 404, 404)})
        check = RegistryCheck(registry, min_interval=0)
        self.assertEqual("registered", check.check("10.5555/fine")["verdict"])
        self.assertEqual("not-registered", check.check("10.5555/gone")["verdict"])
        self.assertEqual("registered-endpoint-artefact", check.check("10.5555/odd")["verdict"])
        blocked = check.check("10.5555/blocked")
        self.assertEqual("inconclusive", blocked["verdict"])
        self.assertEqual({"denied", "rate-limited", "not-found"}, {v["outcome"] for v in blocked["endpoints"].values()})
        self.assertEqual("malformed", check.check("not-a-doi")["verdict"])
        check.check("10.5555/<old>;x#1")
        self.assertTrue(any("%3Cold%3E;x%231" in url for _, url in registry.calls))
        self.assertTrue(all(method == "HEAD" for method, url in registry.calls if not url.startswith("https://doi.org/api")))

    def test_policy_authorizes_identifier_only_before_any_request(self):
        seen = []
        registry = FakeRegistry()
        RegistryCheck(registry, authorize=lambda *call: seen.append((call, len(registry.calls))), min_interval=0).check("10.5555/a")
        self.assertEqual([(("doi.org", "doi_registry_check", {"doi": "10.5555/a"}), 0)], seen)


class StatusTests(unittest.TestCase):
    def test_crossref_relations_give_dated_notices(self):
        record = {"rawMetadata": {"updated-by": [
            {"DOI": "10.5555/notice.1", "type": "retraction", "source": "retraction-watch", "updated": {"date-parts": [[2021, 3, 4]]}},
            {"DOI": "10.5555/notice.2", "type": "new_version", "updated": {"date-parts": [[2020]]}},
            {"DOI": "10.5555/notice.3", "type": "correction", "updated": {"date-parts": [[2019, 11]]}}]}}
        notices = crossref_notices(record)
        self.assertEqual([("retraction", "2021-03-04", "crossref:retraction-watch"), ("correction", "2019-11", "crossref")],
                         [(n["kind"], n["date"], n["source"]) for n in notices])

    def test_retraction_watch_dataset_is_versioned_and_matched_by_doi_or_pmid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = retraction_watch_csv(Path(directory) / "rw-synthetic.csv", [
                {"Record ID": "1", "OriginalPaperDOI": "10.5555/A", "OriginalPaperPubMedID": "111", "RetractionNature": "Retraction",
                 "RetractionDate": "3/24/2021 0:00", "RetractionDOI": "10.5555/a.r", "Reason": "+Duplication of Data;+Error in Analyses;"},
                {"Record ID": "2", "OriginalPaperDOI": "10.5555/a", "RetractionNature": "Reinstatement", "RetractionDate": "6/1/2022 0:00"},
                {"Record ID": "3", "OriginalPaperDOI": "unavailable", "OriginalPaperPubMedID": "222",
                 "RetractionNature": "Expression of concern", "RetractionDate": "1/2/2020 0:00"},
            ])
            dataset = RetractionWatchDataset(path, declared_version="synthetic-2026-10")
        self.assertEqual(3, dataset.version.rows)
        self.assertEqual(64, len(dataset.version.sha256))
        self.assertTrue(dataset.version.identifier.startswith("retraction-watch@synthetic-2026-10#"))
        notices = dataset.notices(doi="10.5555/a")
        self.assertEqual([("retraction", "2021-03-24"), ("reinstatement", "2022-06-01")], [(n["kind"], n["date"]) for n in notices])
        self.assertEqual(["Duplication of Data", "Error in Analyses"], notices[0]["reasons"])
        self.assertEqual(["expression-of-concern"], [n["kind"] for n in dataset.notices(pmid="222")])
        status = combine_status(notices=notices, datasets=[dataset.version], dataset_checked=True)
        self.assertEqual("reinstated", status["state"])
        self.assertEqual("no-notice-found", combine_status(datasets=[dataset.version], dataset_checked=True)["state"])
        self.assertEqual("unknown", combine_status()["state"])
        self.assertEqual("unknown", combine_status(scholarly={"state": "unknown", "signals": [], "limitations": ["x"]})["state"])


class MetadataTests(unittest.TestCase):
    PRINTED = "Stephenson, B., & Stone, C. (2020). External assessment of a transport biomarker. Example Methods, 12(3), 45-67."

    def test_agreement_and_each_disagreement(self):
        record = {"title": "External assessment of a transport biomarker", "authors": ["Bea Stephenson", "Cal Stone"], "year": 2020,
                  "volume": "12", "pages": "45-67", "doi": "10.5555/r", "provider": "crossref"}
        self.assertEqual([], metadata_findings("e1", self.PRINTED, record))
        self.assertEqual(("12", "45"), printed_volume_page(self.PRINTED))
        self.assertGreaterEqual(title_coverage(record["title"], self.PRINTED), 0.99)
        different = {**record, "title": "Unrelated chemistry of rare earth catalysts"}
        variant = {**record, "authors": ["Bea Stephensen", "Cal Stone"]}
        absent = {**record, "authors": ["Dan Hill"]}
        year = {**record, "year": 2016}
        pages = {**record, "volume": "13", "pages": "100-110"}
        fields = {name: [d["field"] for d in metadata_findings("e1", self.PRINTED, value)[0]["detail"]["differences"]]
                  for name, value in (("different", different), ("variant", variant), ("absent", absent), ("year", year), ("pages", pages))}
        self.assertEqual({"different": ["work"], "variant": ["first-author"], "absent": ["first-author"], "year": ["year"],
                          "pages": ["volume", "first-page"]}, fields)
        note = metadata_findings("e1", self.PRINTED, variant)[0]["detail"]["differences"][0]["note"]
        self.assertEqual("near spelling variant", note)
        impossible = metadata_findings("e1", self.PRINTED.replace("2020", "2031"), {**record, "year": 2031}, reference_year=2026)
        self.assertIn("impossible-year", [item["class"] for item in impossible])


class CheckerTests(unittest.TestCase):
    def test_entry_check_persists_scholarly_status_with_dataset_version(self):
        records = {"10.5555/retracted": crossref_message("10.5555/retracted", "A retracted synthetic trial of things",
                                                         [("Ann", "Rivers")], 2019, volume="3", page="10-20",
                                                         updated_by=[{"DOI": "10.5555/retracted.notice", "type": "retraction",
                                                                      "updated": {"date-parts": [[2021, 5, 6]]}, "source": "publisher"}]),
                   "10.5555/fine": crossref_message("10.5555/fine", "A fine synthetic cohort of people", [("Bo", "Stone")], 2018)}
        transport = OfflineScholarlyTransport(records)
        client = ScholarlyClient(plan=EgressPlan(), transport=transport)
        with tempfile.TemporaryDirectory() as directory:
            dataset = RetractionWatchDataset(retraction_watch_csv(Path(directory) / "rw.csv", [
                {"Record ID": "9", "OriginalPaperDOI": "10.5555/retracted", "RetractionNature": "Retraction",
                 "RetractionDate": "5/6/2021 0:00", "RetractionDOI": "10.5555/retracted.notice"}]), declared_version="v1")
        checker = IntegrityChecker(registry=RegistryCheck(FakeRegistry({"10.5555/missing": (404, 404, 404)}), min_interval=0),
                                   scholarly=client, datasets=[dataset], reference_year=2026)
        retracted = checker.check_entry({"id": "ref-00001", "raw": "Rivers, A. (2019). A retracted synthetic trial of things. J, 3, 10-20.",
                                         "identifiers": {"doi": ["10.5555/retracted"], "pmid": [], "arxiv": []}})
        self.assertEqual("retracted", retracted["status"]["state"])
        dates = sorted({(n["source"].split(":")[0], n["date"]) for n in retracted["status"]["notices"]})
        self.assertEqual([("crossref", "2021-05-06"), ("retraction-watch", "2021-05-06")], dates)
        self.assertEqual(["retracted"], [f["class"] for f in retracted["findings"]])
        self.assertEqual(dataset.version.identifier, retracted["findings"][0]["datasetVersion"])
        self.assertEqual("get_publication_status", retracted["scholarly"]["operation"])
        fine = checker.check_entry({"id": "ref-00002", "raw": "Stone, B. (2018). A fine synthetic cohort of people. J.",
                                    "identifiers": {"doi": ["10.5555/fine"], "pmid": [], "arxiv": []}})
        self.assertEqual("no-notice-found", fine["status"]["state"])
        self.assertEqual([], fine["findings"])
        missing = checker.check_entry({"id": "ref-00003", "raw": "Gone, C. (2017). Lost. J.",
                                       "identifiers": {"doi": ["10.5555/missing"], "pmid": [], "arxiv": []}})
        self.assertEqual(["doi-not-registered"], [f["class"] for f in missing["findings"]])
        self.assertNotIn("status", missing)
        no_identifier = checker.check_entry({"id": "ref-00004", "raw": "Plain, D. (2016). No identifier. J.", "identifiers": {}})
        self.assertEqual("unknown", no_identifier["status"]["state"])
        sent = [call["url"] for call in transport.calls]
        self.assertTrue(all("Plain" not in url and "No identifier" not in url for url in sent))


if __name__ == "__main__":
    unittest.main()
