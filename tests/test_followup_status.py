# SPDX-License-Identifier: Apache-2.0
"""Status observations and status-resolution@1: every source mapped from fixtures, unknown on provider failures,
none scoped by what a source covers, reinstatement and date precision, the local Retraction Watch import, one row
per cited work, no re-resolution after the freeze, and no contact identity in any request. No test reaches the
network: the HTTPS opener of the scholarly transport is replaced by a failing stub."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.parse
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import followup_fixtures as F
from ocsl_review import followup_net, followup_status as S
from ocsl_review.cli import main
from ocsl_review.errors import ValidationError
from ocsl_review.followup_branch import FollowupBranch
from ocsl_review.followup_model import StatusObservation, WorkRef

CROSSREF = "https://api.crossref.org/works"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
RW_CSV = F.FIXTURES / "status" / "retraction-watch-snapshot.csv"


def no_network(*args, **kwargs):
    raise AssertionError("a follow-up test tried to open a network connection")


def crossref_work(doi: str) -> str:
    return f"{CROSSREF}/{urllib.parse.quote(doi, safe='')}"


def crossref_updates(doi: str) -> str:
    return CROSSREF + "?" + urllib.parse.urlencode({"filter": f"updates:{doi}", "rows": S.CROSSREF_ROWS})


def epmc_search(doi: str) -> str:
    return EPMC + "?" + urllib.parse.urlencode({"query": f'DOI:"{doi}"', "pageSize": 5, "resultType": "core",
                                                "format": "json"})


def efetch(pmid: str) -> str:
    return f"{EUTILS}/efetch.fcgi?" + urllib.parse.urlencode({"db": "pubmed", "id": pmid, "retmode": "xml",
                                                              "tool": "ocsl_review"})


def esummary(pmid: str) -> str:
    return f"{EUTILS}/esummary.fcgi?" + urllib.parse.urlencode({"db": "pubmed", "id": pmid, "retmode": "json",
                                                                "tool": "ocsl_review"})


def status_routes() -> dict:
    """Provider answers for the synthetic cited works (anything else answers 404)."""
    routes = {
        crossref_work("10.5555/fu.work.retracted"): (200, "status/crossref-work-retracted.json"),
        crossref_updates("10.5555/fu.work.retracted"): (200, "status/crossref-updates-retracted.json"),
        crossref_work("10.5555/fu.work.concern"): (200, "status/crossref-work-concern.json"),
        crossref_work("10.5555/fu.work.corrected"): (200, "status/crossref-work-corrected.json"),
        crossref_work("10.5555/fu.work.plain1"): (200, "status/crossref-work-plain.json"),
        crossref_work("10.5555/fu.work.plain2"): (200, "status/crossref-work-reinstated.json"),
        efetch("9000001"): (200, "status/pubmed-9000001.xml"),
        efetch("9000002"): (200, "status/pubmed-9000002.xml"),
        efetch("9000003"): (200, "status/pubmed-9000003.xml"),
        efetch("9000004"): (200, "status/pubmed-9000004.xml"),
        esummary("9100001"): (200, "status/pubmed-esummary-9100001.json"),
        epmc_search("10.5555/fu.work.retracted"): (200, "status/europe-pmc-retracted.json"),
        epmc_search("10.5555/fu.work.plain1"): (200, "status/europe-pmc-plain.json"),
    }
    for doi in ("10.5555/fu.work.concern", "10.5555/fu.work.corrected", "10.5555/fu.work.plain1",
                "10.5555/fu.work.plain2"):
        routes[crossref_updates(doi)] = (200, "status/crossref-updates-none.json")
    return routes


def clean_environment() -> dict:
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("OCSL_REVIEW_CONTACT_EMAIL", "NCBI_API_KEY")}
    environment.update(EMAIL="someone@example.org", GIT_AUTHOR_EMAIL="someone@example.org")
    return environment


def client(transport):
    return followup_net.client(transport=transport, sleep=lambda seconds: None)


class StatusTestCase(unittest.TestCase):
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
        for patcher in (mock.patch("ocsl_review.scholarly.open_public_https", side_effect=no_network),
                        mock.patch.dict(os.environ, clean_environment(), clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        type(self).count += 1
        self.path = self.root / f"branch-{self.count}.sqlite3"
        FollowupBranch.create(self.path, self.census["parent"], protocol=F.protocol())
        self.branch = FollowupBranch(self.path)

    def session(self, routes=None):
        transport = F.FixtureTransport(status_routes() if routes is None else routes)
        connection = self.branch.connect(write=True)
        self.addCleanup(connection.close)
        return S.ProviderSession(client(transport), S.Recorder(connection)), transport, connection


class SourceMappingTests(StatusTestCase):
    def test_crossref_notices_keep_their_origin_date_and_precision(self):
        session, _, _ = self.session()
        source = S.CrossrefStatus(session)
        retracted = source.observe(WorkRef("w-retracted", "10.5555/fu.work.retracted"), snapshot_id="s")
        self.assertEqual({("crossref:retraction-watch", "retracted", "10.5555/fu.notice.retraction1", "2022-03-04",
                           "day"),
                          ("crossref:publisher", "retracted", "10.5555/fu.notice.retraction1", "2022-03-04", "day")},
                         {(o.source, o.status, o.notice_doi, o.notice_date, o.date_precision) for o in retracted})
        self.assertEqual(2, len(retracted), "the publisher notice of the work and of the updates query count once")
        self.assertEqual(900001, next(o for o in retracted if o.source == "crossref:retraction-watch").detail["recordId"])
        concern = source.observe(WorkRef("w-concern", "10.5555/fu.work.concern"), snapshot_id="s")
        self.assertEqual([("expression-of-concern", "2021-07", "month")],
                         [(o.status, o.notice_date, o.date_precision) for o in concern])
        corrected = source.observe(WorkRef("w-corrected", "10.5555/fu.work.corrected"), snapshot_id="s")
        self.assertEqual([("corrected", "2019-01-15", "day", "correction")],
                         [(o.status, o.notice_date, o.date_precision, o.notice_type) for o in corrected])
        plain = source.observe(WorkRef("w-plain-1", "10.5555/fu.work.plain1"), snapshot_id="s")
        self.assertEqual([("none", ("retraction", "expression-of-concern", "correction", "reinstatement"))],
                         [(o.status, o.covers) for o in plain])
        reinstated = source.observe(WorkRef("w-plain-2", "10.5555/fu.work.plain2"), snapshot_id="s")
        self.assertEqual({("retracted", "2020-05", "month"), ("reinstated", "2021-02-10", "day")},
                         {(o.status, o.notice_date, o.date_precision) for o in reinstated})

    def test_crossref_needs_both_answers_for_none(self):
        routes = status_routes()
        routes[crossref_updates("10.5555/fu.work.plain1")] = (503, b"{}")
        session, _, _ = self.session(routes)
        (observation,) = S.CrossrefStatus(session).observe(WorkRef("w-plain-1", "10.5555/fu.work.plain1"),
                                                            snapshot_id="s")
        self.assertEqual(("unknown", "updates-query-http-503"), (observation.status, observation.detail["reason"]))
        (missing,) = S.CrossrefStatus(session).observe(WorkRef("w-plain-3", "10.5555/fu.work.plain3"),
                                                        snapshot_id="s")
        self.assertEqual(("unknown", "not-found"), (missing.status, missing.detail["reason"]))
        (nodoi,) = S.CrossrefStatus(session).observe(WorkRef("w-x"), snapshot_id="s")
        self.assertEqual(("unknown", "no-doi"), (nodoi.status, nodoi.detail["reason"]))

    def test_pubmed_comments_corrections_and_notice_dates(self):
        session, _, _ = self.session()
        source = S.PubMedStatus(session)
        (retracted,) = source.observe(WorkRef("w-retracted", pmid="9000001"), snapshot_id="s")
        self.assertEqual(("retracted", "9100001", "RetractionIn", "2022-03-04", "day"),
                         (retracted.status, retracted.notice_pmid, retracted.notice_type, retracted.notice_date,
                          retracted.date_precision))
        (concern,) = source.observe(WorkRef("w-concern", pmid="9000002"), snapshot_id="s")
        self.assertEqual(("expression-of-concern", "9100002", None), (concern.status, concern.notice_pmid,
                                                                      concern.notice_date))
        self.assertEqual("unavailable", concern.detail["noticeDate"])
        (corrected,) = source.observe(WorkRef("w-corrected", pmid="9000003"), snapshot_id="s")
        self.assertEqual(("corrected", "ErratumIn"), (corrected.status, corrected.notice_type))
        (none,) = source.observe(WorkRef("w-plain-1", pmid="9000004"), snapshot_id="s")
        self.assertEqual(("none", ("retraction", "expression-of-concern", "correction")), (none.status, none.covers))
        (missing,) = source.observe(WorkRef("w-plain-2", pmid="9000005"), snapshot_id="s")
        self.assertEqual(("unknown", "http-404"), (missing.status, missing.detail["reason"]))
        (nopmid,) = source.observe(WorkRef("w-plain-3", "10.5555/fu.work.plain3"), snapshot_id="s")
        self.assertEqual(("unknown", "no-pmid"), (nopmid.status, nopmid.detail["reason"]))

    def test_europe_pmc_flag_covers_retraction_only(self):
        session, _, _ = self.session()
        source = S.EuropePmcStatus(session)
        (retracted,) = source.observe(WorkRef("w-retracted", "10.5555/fu.work.retracted"), snapshot_id="s")
        self.assertEqual(("retracted", None, None, ("retraction",)),
                         (retracted.status, retracted.notice_doi, retracted.notice_date, retracted.covers))
        (plain,) = source.observe(WorkRef("w-plain-1", "10.5555/fu.work.plain1"), snapshot_id="s")
        self.assertEqual(("none", ("retraction",)), (plain.status, plain.covers))
        (failed,) = source.observe(WorkRef("w-concern", "10.5555/fu.work.concern"), snapshot_id="s")
        self.assertEqual("unknown", failed.status)

    def test_provider_failures_are_unknown_observations_never_exceptions(self):
        routes = {crossref_work("10.5555/fu.work.retracted"): (503, b"{}"),
                  crossref_updates("10.5555/fu.work.retracted"): (200, b"not json"),
                  efetch("9000001"): (200, b"<PubmedArticleSet/>"),
                  epmc_search("10.5555/fu.work.retracted"): (200, b"{\"resultList\": 7}")}
        session, _, connection = self.session(routes)
        work = WorkRef("w-retracted", "10.5555/fu.work.retracted", "9000001")
        for source in (S.CrossrefStatus(session), S.PubMedStatus(session), S.EuropePmcStatus(session)):
            with self.subTest(source=source.name):
                (observation,) = source.observe(work, snapshot_id="s")
                self.assertEqual("unknown", observation.status)
                self.assertIsNone(observation.notice_doi)
        outcomes = dict(connection.execute("SELECT provider, COUNT(*) FROM fu_requests GROUP BY provider").fetchall())
        self.assertEqual({"crossref": 2, "pubmed": 1, "europe-pmc": 1}, outcomes)
        errors = {row[0] for row in connection.execute("SELECT error FROM fu_requests WHERE outcome!='ok'")}
        self.assertEqual({"http-503"}, errors)


class ResolutionRuleTests(unittest.TestCase):
    def observation(self, source, status, date=None, precision=None, covers=None, doi=None):
        covers = covers or {"europe-pmc": ("retraction",), "pubmed": S.DECIDING_KINDS}.get(source, S.NOTICE_KINDS)
        return StatusObservation("w", source, status, "2026-01-01T00:00:00Z", "s", tuple(covers), notice_doi=doi,
                                 notice_date=date, date_precision=precision)

    def resolve(self, *observations):
        return S.resolve_work("w", list(observations), snapshot_id="s", resolved_at="2026-01-02T00:00:00Z")

    def test_europe_pmc_alone_never_makes_a_work_none(self):
        result = self.resolve(self.observation("europe-pmc", "none"))
        self.assertEqual(("unknown", ["correction", "expression-of-concern"]), (result.status,
                                                                               list(result.unknown_kinds)))
        result = self.resolve(self.observation("europe-pmc", "none"), self.observation("pubmed", "none"))
        self.assertEqual(("none", []), (result.status, list(result.unknown_kinds)))
        result = self.resolve(self.observation("crossref", "unknown"))
        self.assertEqual(("unknown", ["correction", "expression-of-concern", "retraction"]),
                         (result.status, list(result.unknown_kinds)))

    def test_the_retraction_watch_snapshot_alone_covers_no_correction(self):
        """Retraction Watch records corrections only selectively: by default its silence covers retractions,
        expressions of concern and reinstatements; a correction counts as covered through Crossref or PubMed, or when
        the protocol declares it for the source. The basis is recorded per source."""
        result = self.resolve(self.observation("retraction-watch-csv", "none"))
        self.assertEqual(("unknown", ["correction"]), (result.status, list(result.unknown_kinds)))
        entry = result.sources[0]
        self.assertEqual((["expression-of-concern", "reinstatement", "retraction"], "default"),
                         (entry["covers"], entry["coversBasis"]))
        self.assertIn("correction", entry["reports"])
        both = self.resolve(self.observation("retraction-watch-csv", "none"), self.observation("crossref", "none"))
        self.assertEqual("none", both.status)
        declared = S.source_covers({"status": {"covers": {"retraction-watch-csv": list(S.NOTICE_KINDS)}}})
        alone = S.resolve_work("w", [self.observation("retraction-watch-csv", "none")], snapshot_id="s",
                               resolved_at="2026-01-02T00:00:00Z", covers=declared)
        self.assertEqual(("none", "protocol"), (alone.status, alone.sources[0]["coversBasis"]))
        narrowed = S.source_covers({"status": {"covers": {"crossref": ["retraction", "expression-of-concern"]}}})
        crossref = S.resolve_work("w", [self.observation("crossref", "none")], snapshot_id="s",
                                  resolved_at="2026-01-02T00:00:00Z", covers=narrowed)
        self.assertEqual(("unknown", ["correction"]), (crossref.status, list(crossref.unknown_kinds)))
        # A correction notice the snapshot does list still decides.
        self.assertEqual("corrected", self.resolve(self.observation("retraction-watch-csv", "corrected", "2020-01-02",
                                                                     "day", doi="10.1/c")).status)

    def test_a_later_reinstatement_reverses_the_retraction_and_the_next_kind_decides(self):
        result = self.resolve(self.observation("crossref:publisher", "retracted", "2020-05", "month", doi="10.1/r"),
                              self.observation("crossref:publisher", "reinstated", "2021-02-10", "day"),
                              self.observation("pubmed", "corrected", "2019-01-01", "day"))
        self.assertEqual(("corrected", True, "2019-01-01"), (result.status, result.reinstated, result.notice_date))
        undated = self.resolve(self.observation("europe-pmc", "retracted"),
                               self.observation("crossref:publisher", "reinstated", "2021-02-10", "day"))
        self.assertEqual(("retracted", False), (undated.status, undated.reinstated),
                         "a reinstatement cannot be later than a retraction without a date")
        same_year = self.resolve(self.observation("crossref:publisher", "retracted", "2021-03-04", "day"),
                                 self.observation("retraction-watch-csv", "reinstated", "2021", "year"))
        self.assertEqual(("retracted", False), (same_year.status, same_year.reinstated),
                         "a reinstatement in the same year as the retraction is not shown to be later")

    def test_the_deciding_notice_is_the_earliest_at_its_precision(self):
        result = self.resolve(self.observation("crossref:publisher", "expression-of-concern", "2021-07", "month",
                                               doi="10.1/a"),
                              self.observation("retraction-watch-csv", "expression-of-concern", "2021-07-01", "day",
                                               doi="10.1/b"),
                              self.observation("pubmed", "expression-of-concern"))
        self.assertEqual(("expression-of-concern", "2021-07-01", "day", "retraction-watch-csv"),
                         (result.status, result.notice_date, result.date_precision, result.decided_by))
        result = self.resolve(self.observation("pubmed", "retracted"),
                              self.observation("crossref:publisher", "retracted", "2022", "year"),
                              self.observation("europe-pmc", "corrected", "2020-01-01", "day"))
        self.assertEqual(("retracted", "2022", "crossref:publisher"), (result.status, result.notice_date,
                                                                       result.decided_by))

    def test_dates_compare_at_the_coarser_precision(self):
        self.assertEqual(0, S.compare_dates("2022", "year", "2022-03-04", "day"))
        self.assertEqual(1, S.compare_dates("2022-04", "month", "2022-03-31", "day"))
        self.assertEqual(-1, S.compare_dates("2021-12-31", "day", "2022", "year"))
        self.assertIsNone(S.compare_dates(None, None, "2022", "year"))
        self.assertEqual(("2022-03-04", "day"), S.pubmed_date("2022 Mar 4"))
        self.assertEqual(("2022-03", "month"), S.pubmed_date("2022 Mar-Apr"))
        self.assertEqual(("2022", "year"), S.pubmed_date("2022 Spring"))
        self.assertEqual(("2021-02", "month"), S.date_parts({"date-parts": [[2021, 2, 30]]}))
        self.assertEqual((None, None), S.date_parts({"date-parts": [[None]]}))


class SnapshotTests(StatusTestCase):
    def fetch(self, transport, **options):
        with self.branch.writing() as connection:
            return S.fetch_status(connection, client(transport), sources=["crossref", "pubmed", "europe-pmc"],
                                  **options)

    def test_the_retraction_watch_import_is_local_and_registers_its_snapshot(self):
        with self.branch.writing() as connection:
            result = S.import_retraction_watch(connection, RW_CSV, "synthetic-snapshot", protocol=F.protocol())
            again = S.import_retraction_watch(connection, RW_CSV, "synthetic-snapshot", protocol=F.protocol())
            dataset = connection.execute("SELECT * FROM fu_datasets WHERE kind='retraction-watch-csv'").fetchall()
            rows = connection.execute("SELECT work_key, status, notice_doi, notice_pmid, notice_date, date_precision "
                                      "FROM fu_status_observations WHERE status!='none'").fetchall()
            requests = connection.execute("SELECT COUNT(*) FROM fu_requests").fetchone()[0]
        self.assertEqual(1, len(dataset))
        self.assertEqual((3, "retraction-watch-snapshot.csv", "synthetic-snapshot"),
                         (dataset[0]["rows"], dataset[0]["file_name"], dataset[0]["declared_version"]))
        self.assertEqual(result["snapshot"], dataset[0]["dataset_id"])
        self.assertEqual(0, again["written"], "importing the same snapshot again adds nothing")
        self.assertEqual({("w-retracted", "retracted", "10.5555/fu.notice.retraction1", "9100001", "2022-03-04", "day"),
                          ("w-concern", "expression-of-concern", "10.5555/fu.notice.concern1", "9100002",
                           "2021-07-01", "day")}, {tuple(row) for row in rows})
        self.assertEqual({"expression-of-concern": 1, "none": 5, "retracted": 1}, result["statuses"])
        self.assertEqual(0, requests)
        with self.branch.writing() as connection:
            with self.assertRaisesRegex(ValidationError, "declared version"):
                S.import_retraction_watch(connection, RW_CSV, "another-version", protocol=F.protocol())
            with self.assertRaisesRegex(ValidationError, "sha256"):
                S.import_retraction_watch(connection, RW_CSV, "synthetic-snapshot", protocol=F.protocol(
                    status={"sources": ["retraction-watch-csv"], "rule": "status-resolution@1",
                            "retractionWatchCsv": {"declaredVersion": "synthetic-snapshot", "sha256": "0" * 64}}))

    def test_every_cited_work_gets_one_status_row_with_source_and_date(self):
        transport = F.FixtureTransport(status_routes())
        fetched = self.fetch(transport)
        self.assertTrue(fetched["snapshot"].startswith("status-"))
        self.assertEqual({"crossref": 7, "pubmed": 7, "europe-pmc": 7}, fetched["works"])
        with self.branch.writing() as connection:
            resolved = S.resolve_status(connection, protocol=F.protocol())
            rows = {row["work_key"]: dict(row) for row in connection.execute("SELECT * FROM fu_work_status")}
            snapshot = connection.execute("SELECT * FROM fu_datasets WHERE kind='status-snapshot'").fetchone()
        self.assertEqual(len(F.WORKS), len(rows))
        self.assertEqual(fetched["snapshotRows"], snapshot["rows"])
        self.assertEqual(("retracted", "2022-03-04", "crossref:retraction-watch", "10.5555/fu.notice.retraction1"),
                         tuple(rows["w-retracted"][key] for key in ("status", "notice_date", "decided_by",
                                                                    "notice_doi")))
        self.assertEqual(("expression-of-concern", "2021-07", "month"),
                         tuple(rows["w-concern"][key] for key in ("status", "notice_date", "date_precision")))
        self.assertEqual("corrected", rows["w-corrected"]["status"])
        self.assertEqual("none", rows["w-plain-1"]["status"])
        self.assertEqual(("none", 1, []), (rows["w-plain-2"]["status"], rows["w-plain-2"]["reinstated"],
                                           json.loads(rows["w-plain-2"]["unknown_kinds_json"])),
                         "Crossref answered and covers every kind; its later reinstatement reverses the retraction")
        self.assertEqual(("unknown", ["correction", "expression-of-concern", "retraction"]),
                         (rows["w-plain-3"]["status"], json.loads(rows["w-plain-3"]["unknown_kinds_json"])))
        self.assertEqual(0, self.branch.verify()["citedWorksWithoutStatusRow"])
        self.assertEqual({"both": 0, "flagged": 0}, {key: resolved["censusRetractedFlag"][key]
                                                      for key in ("both", "flagged")})
        with self.branch.writing() as connection:
            S.import_retraction_watch(connection, RW_CSV, "synthetic-snapshot", protocol=F.protocol())
            S.resolve_status(connection, protocol=F.protocol())
            rows = {row["work_key"]: dict(row) for row in connection.execute("SELECT * FROM fu_work_status")}
        self.assertEqual(("none", 1), (rows["w-plain-2"]["status"], rows["w-plain-2"]["reinstated"]),
                         "the Retraction Watch snapshot covers every notice kind")
        self.assertEqual(("expression-of-concern", "2021-07-01", "retraction-watch-csv"),
                         tuple(rows["w-concern"][key] for key in ("status", "notice_date", "decided_by")))
        self.assertIn("+", rows["w-plain-1"]["snapshot_id"])
        sources = {entry["source"]: entry for entry in json.loads(rows["w-retracted"]["sources_json"])}
        self.assertTrue(sources["pubmed"]["answered"])
        self.assertEqual("9100001", sources["pubmed"]["notices"][0]["pmid"])

    def test_requests_carry_no_contact_identity(self):
        transport = F.FixtureTransport(status_routes())
        self.fetch(transport)
        self.assertTrue(transport.requests)
        for request in transport.requests:
            self.assertNotIn("@", json.dumps(request))
            self.assertNotIn("mailto", json.dumps(request).lower())
        with closing(self.branch.connect()) as connection:
            for row in connection.execute("SELECT url, params_json, error FROM fu_requests"):
                self.assertNotIn("@", json.dumps(list(row)))
                self.assertNotIn("mailto", json.dumps(list(row)).lower())

    def test_a_snapshot_is_continued_where_it_stopped(self):
        transport = F.FixtureTransport(status_routes())
        first = self.fetch(transport, limit=2)
        self.assertEqual({"crossref": 2, "pubmed": 2, "europe-pmc": 2}, first["works"])
        continued = self.fetch(F.FixtureTransport(status_routes()), snapshot=first["snapshot"])
        self.assertEqual(first["snapshot"], continued["snapshot"])
        # The first two works answered at Crossref and PubMed; Europe PMC knows neither (no fixture: unknown), so
        # every work is asked there again.
        self.assertEqual({"crossref": 5, "pubmed": 5, "europe-pmc": 7}, continued["works"])
        with self.assertRaisesRegex(ValidationError, "no observations"):
            self.fetch(transport, snapshot="status-20200101T000000000000Z")

    def test_resolution_is_refused_after_the_freeze(self):
        with self.branch.writing() as connection:
            S.import_retraction_watch(connection, RW_CSV, "synthetic-snapshot", protocol=F.protocol())
            S.resolve_status(connection, protocol=F.protocol())
            connection.execute("INSERT INTO fu_protocol_freeze VALUES('freeze-1', ?, ?, 's', ?, '[]', '{}', ?)",
                               ("a" * 64, "b" * 64, "c" * 64, "2026-01-01T00:00:00Z"))
        with self.branch.writing() as connection:
            with self.assertRaisesRegex(ValidationError, "frozen"):
                S.resolve_status(connection, protocol=F.protocol())


class CommandTests(StatusTestCase):
    def invoke(self, *argv):
        target = self.root / f"result-{self.count}-{len(argv)}-{argv[1]}.json"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["--result-file", str(target), *argv])
        return code, json.loads(target.read_text(encoding="utf-8"))

    def test_status_commands_write_result_files(self):
        code, record = self.invoke("followup", "status-import", "--database", str(self.path), "--csv", str(RW_CSV),
                                   "--declared-version", "synthetic-snapshot")
        self.assertEqual(0, code, record)
        transport = F.FixtureTransport(status_routes())
        with mock.patch.object(S, "make_client", lambda workspace=None: client(transport)):
            code, record = self.invoke("followup", "status-fetch", "--database", str(self.path), "--source", "crossref",
                                       "--limit", "3")
        self.assertEqual((0, {"crossref": 3}), (code, record["result"]["works"]), record)
        code, record = self.invoke("followup", "status-resolve", "--database", str(self.path))
        self.assertEqual(0, code, record)
        self.assertEqual(0, record["result"]["citedWorksWithoutStatusRow"])
        self.assertEqual(len(F.WORKS), record["result"]["works"])
        code, record = self.invoke("followup", "status-import", "--database", str(self.path), "--csv",
                                   str(self.root / "missing.csv"), "--declared-version", "x")
        self.assertEqual("validation", record["error"]["kind"])


if __name__ == "__main__":
    unittest.main()
