# SPDX-License-Identifier: Apache-2.0
"""The follow-up frame, strata, inclusion probabilities, pilot, replicate selection and protocol freeze.

The helpers at the top prepare branches over the synthetic parent census (tests/followup_fixtures) and are shared by
the other analysis test modules. Status rows, which the evidence commands write in a study, are inserted directly.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest import mock

import followup_fixtures as F
from ocsl_review import followup_stats, followup_strata
from ocsl_review.errors import ValidationError
from ocsl_review.followup_branch import FollowupBranch, require_freeze
from ocsl_review.followup_model import claim_id, locus_id, sentence_digest

A1, A2, B1 = "10.5555/fu.review.a1", "10.5555/fu.review.a2", "10.5555/fu.review.b1"
# work key -> (status, notice date, precision); every other parent work is "none".
STATUSES = {"w-retracted": ("retracted", "2021-06-01", "day"),
            "w-concern": ("expression-of-concern", "2023-06", "month"),
            "w-corrected": ("corrected", None, None)}


def lid(journal: str, doi: str, label: str, seq: int = 1) -> str:
    return locus_id(journal, doi, label, seq)


def insert_statuses(branch: FollowupBranch, statuses: Mapping[str, tuple] = STATUSES, *, snapshot: str = "snapshot-1",
                    default: str = "none", skip: tuple[str, ...] = ()) -> None:
    """One fu_work_status row per parent work (as followup status-resolve writes them)."""
    with branch.writing() as connection:
        works = [row[0] for row in connection.execute("SELECT work_key FROM census.works ORDER BY work_key")]
        for work in works:
            if work in skip:
                continue
            status, notice, precision = statuses.get(work, (default, None, None))
            connection.execute(
                "INSERT INTO fu_work_status(work_key, status, reinstated, notice_doi, notice_date, date_precision, "
                "decided_by, sources_json, unknown_kinds_json, checked_at, snapshot_id, rule_version, resolved_at) "
                "VALUES(?,?,0,?,?,?,?,?,?,?,?,?,?)",
                (work, status, f"10.5555/notice.{work}" if notice else None, notice, precision,
                 "crossref:publisher" if status not in ("none", "unknown") else None, json.dumps(["crossref"]), "[]",
                 "2026-01-01T00:00:00Z", snapshot, "status-resolution@1", "2026-01-01T00:00:00Z"))


def synthetic_parent(root: Path, *, reviews=None, works=None, citations=None) -> dict[str, Path]:
    """A frozen parent census built by the fixture builder from other synthetic reviews, works and citations."""
    root.mkdir(parents=True, exist_ok=True)
    changes = {name: value for name, value in (("REVIEWS", reviews), ("WORKS", works), ("CITATIONS", citations))
               if value is not None}
    with mock.patch.multiple(F, **changes):
        return F.make_parent(root)


def make_branch(root: Path, name: str, protocol: Mapping[str, Any] | None = None, *, census=None,
                text: bool = False) -> FollowupBranch:
    if census is None:
        (root / (name + "-census")).mkdir(parents=True, exist_ok=True)
        census = F.make_parent(root / (name + "-census"))
    path = root / name
    FollowupBranch.create(path, census["parent"], parent_text=census["text"] if text else None,
                          protocol=protocol or F.protocol())
    return FollowupBranch(path)


def build_strata(branch: FollowupBranch, *, freeze: bool = False) -> dict[str, Any]:
    followup_stats.cmd_hotspots(SimpleNamespace(database=str(branch.path)))
    return followup_strata.cmd_strata(SimpleNamespace(database=str(branch.path), freeze=freeze))


def frozen_branch(root: Path, name: str, protocol: Mapping[str, Any] | None = None, *, census=None,
                  statuses: Mapping[str, tuple] = STATUSES, text: bool = False) -> FollowupBranch:
    branch = make_branch(root, name, protocol, census=census, text=text)
    insert_statuses(branch, statuses)
    build_strata(branch, freeze=True)
    return branch


def strata_of(branch: FollowupBranch) -> dict[str, dict[str, Any]]:
    with closing(branch.connect()) as connection:
        result: dict[str, dict[str, Any]] = {}
        for row in connection.execute("SELECT * FROM fu_strata"):
            result.setdefault(row["stratum"], {})[row["locus_id"]] = dict(row)
        return result


def protocol_with(**strata: Any) -> dict[str, Any]:
    value = F.protocol()
    for name, spec in strata.items():
        value["strata"][name] = {**value["strata"].get(name, {}), **spec}
    return value


class StrataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        (cls.root / "census").mkdir()
        cls.census = F.make_parent(cls.root / "census")
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def branch(self, protocol=None, *, statuses=STATUSES, text=False) -> FollowupBranch:
        type(self).count += 1
        branch = make_branch(self.root, f"branch-{self.count}.sqlite3", protocol, census=self.census, text=text)
        if statuses is not None:
            insert_statuses(branch, statuses)
        return branch

    def test_frame_has_one_row_per_parent_locus_with_claims_and_provenance(self):
        branch = self.branch()
        build_strata(branch)
        with closing(branch.connect()) as connection:
            frame = {row["locus_id"]: dict(row) for row in connection.execute("SELECT * FROM fu_frame")}
            self.assertEqual(len(F.CITATIONS), len(frame))
            first, second = frame[lid(F.JOURNAL_A, A1, "1")], frame[lid(F.JOURNAL_A, A1, "2")]
            self.assertEqual(first["claim_id"], second["claim_id"])
            sentence = "Marker zeta doubles the risk in adults ⟨R1⟩ ⟨R2⟩ ."
            self.assertEqual(claim_id(F.JOURNAL_A, A1, 3, sentence_digest(sentence)), first["claim_id"])
            self.assertEqual(("w-retracted", 2021, "2021-03-01", "topic-x", "public", "resolved", 1, "true"),
                             (first["work_key"], first["review_year"], first["review_first_online"], first["topic"],
                              first["provenance_class"], first["identity"], first["cited_inbody"],
                              first["census_abs_category"]))
            redacted = frame[lid(F.JOURNAL_A, A2, "2")]
            parent = connection.execute("SELECT sentence FROM census.loci WHERE doi=? AND label='2'", (A2,)).fetchone()[0]
            self.assertTrue(parent.startswith("sha256:"))
            self.assertEqual(parent[len("sha256:"):], redacted["sentence_sha256"])
            self.assertEqual("entitled", redacted["provenance_class"])
            self.assertEqual("false", frame[lid(F.JOURNAL_A, A2, "1")]["census_ft_category"])
        self.assertTrue(branch.verify()["ok"], branch.verify()["problems"])

    def test_s1_holds_loci_citing_compromised_works_and_co_cited_anchors(self):
        branch = self.branch()
        result = build_strata(branch)
        strata = strata_of(branch)
        expected_s1 = {lid(F.JOURNAL_A, A1, "1"), lid(F.JOURNAL_A, A2, "2"), lid(F.JOURNAL_B, B1, "2"),
                       lid(F.JOURNAL_A, A2, "1"), lid(F.JOURNAL_B, B1, "1")}
        self.assertEqual(expected_s1, set(strata["S1"]))
        self.assertTrue(all(row["inclusion_probability"] == 1.0 and row["selection"] == "census"
                            for row in strata["S1"].values()))
        self.assertNotIn("S2", strata)
        with closing(branch.connect()) as connection:
            pairs = {(row[0], row[1]) for row in connection.execute("SELECT anchor_locus_id, locus_id FROM fu_cocited")}
        self.assertEqual({(lid(F.JOURNAL_A, A1, "1"), lid(F.JOURNAL_A, A1, "2")),
                          (lid(F.JOURNAL_B, B1, "2"), lid(F.JOURNAL_B, B1, "3"))}, pairs)
        self.assertEqual({lid(F.JOURNAL_A, A1, "2"), lid(F.JOURNAL_B, B1, "3")}, set(strata["co-cited"]))
        self.assertEqual(5, result["strata"]["S1"])
        self.assertEqual(2, result["cocitedPairs"])

    def test_s1_follows_the_protocols_statuses(self):
        branch = self.branch(protocol_with(S1={"statuses": ["retracted"]}))
        build_strata(branch)
        self.assertEqual({lid(F.JOURNAL_A, A1, "1"), lid(F.JOURNAL_A, A2, "2"), lid(F.JOURNAL_B, B1, "2")},
                         set(strata_of(branch)["S1"]))

    def test_s0_is_frequency_matched_with_realised_probabilities(self):
        branch = self.branch(protocol_with(S0={"size": 3, "matchOn": ["journal", "year"]}))
        result = build_strata(branch)
        s0 = strata_of(branch)["S0"]
        pool_a2021 = {lid(F.JOURNAL_A, A1, "2"), lid(F.JOURNAL_A, A1, "4")}
        self.assertEqual(3, len(s0))
        self.assertEqual(1, len(pool_a2021 & set(s0)))
        self.assertIn(lid(F.JOURNAL_A, A2, "3"), s0)
        self.assertIn(lid(F.JOURNAL_B, B1, "3"), s0)
        chosen = (pool_a2021 & set(s0)).pop()
        self.assertEqual([chosen], followup_strata.seeded_sample(F.PROTOCOL["seed"], ("S0", [F.JOURNAL_A, 2021]),
                                                                 pool_a2021, 1))
        self.assertEqual(0.5, s0[chosen]["inclusion_probability"])
        self.assertEqual({"journal": F.JOURNAL_A, "year": 2021}, json.loads(s0[chosen]["cell"]))
        self.assertEqual(1.0, s0[lid(F.JOURNAL_A, A2, "3")]["inclusion_probability"])
        self.assertTrue(all(row["selection"] == "sampled" for row in s0.values()))
        self.assertEqual((3, 0, 4), (result["s0"]["selected"], result["s0"]["shortfall"], result["s0"]["eligible"]))
        family = {lid(F.JOURNAL_A, A1, "3"), lid(F.JOURNAL_B, B1, "4")}
        self.assertFalse(family & set(s0))

    def test_s0_redistributes_what_the_caps_leave_and_reports_the_shortfall(self):
        branch = self.branch(protocol_with(S0={"size": 5, "matchOn": ["journal", "year"]}))
        result = build_strata(branch)
        self.assertEqual((4, 1), (result["s0"]["selected"], result["s0"]["shortfall"]))
        self.assertTrue(all(row["inclusion_probability"] == 1.0 for row in strata_of(branch)["S0"].values()))

    def test_allocation_by_hand(self):
        cells = {("A", 2021): 1, ("A", 2023): 2, ("B", 2022): 2}
        capacity = {("A", 2021): 2, ("A", 2023): 1, ("B", 2022): 1, ("C", 2020): 9}
        self.assertEqual(({("A", 2021): 0, ("A", 2023): 1, ("B", 2022): 1, ("C", 2020): 0}, 0),
                         followup_strata.allocate(2, cells, capacity))
        self.assertEqual(({("A", 2021): 1, ("A", 2023): 1, ("B", 2022): 1, ("C", 2020): 0}, 0),
                         followup_strata.allocate(3, cells, capacity))
        self.assertEqual(({("A", 2021): 2, ("A", 2023): 1, ("B", 2022): 1, ("C", 2020): 0}, 1),
                         followup_strata.allocate(5, cells, capacity))
        self.assertEqual(({("A", 1): 1, ("B", 1): 3}, 0), followup_strata.allocate(4, {}, {("A", 1): 1, ("B", 1): 3}))
        tenths = {(str(index),): 1 for index in range(10)}
        allocation, _ = followup_strata.allocate(10, tenths, {cell: 5 for cell in tenths})
        self.assertEqual([1] * 10, list(allocation.values()))

    def test_pilot_shares_per_status_and_the_s0_count(self):
        branch = self.branch(protocol_with(S0={"size": 3, "matchOn": ["journal", "year"]}))
        result = build_strata(branch)
        strata = strata_of(branch)
        pilot = {unit: row["pilot_probability"] for unit, row in strata["S1"].items() if row["pilot"]}
        retracted = {lid(F.JOURNAL_A, A1, "1"), lid(F.JOURNAL_A, A2, "2"), lid(F.JOURNAL_B, B1, "2")}
        self.assertEqual(2, len(retracted & set(pilot)))
        self.assertTrue(all(abs(pilot[unit] - 2 / 3) < 1e-12 for unit in retracted & set(pilot)))
        self.assertEqual(1.0, pilot[lid(F.JOURNAL_A, A2, "1")])
        self.assertEqual(1.0, pilot[lid(F.JOURNAL_B, B1, "1")])
        s0_pilot = [row for row in strata["S0"].values() if row["pilot"]]
        self.assertEqual(1, len(s0_pilot))
        self.assertAlmostEqual(s0_pilot[0]["inclusion_probability"] / 3, s0_pilot[0]["pilot_probability"])
        self.assertEqual({"S1": 4, "S0": 1}, result["pilot"])
        self.assertTrue(all(not row["pilot"] and row["pilot_probability"] is None
                            for row in strata.get("co-cited", {}).values()))

    def test_s3_is_dropped_without_verified_licensing_and_built_with_it(self):
        family = {lid(F.JOURNAL_A, A1, "3"), lid(F.JOURNAL_B, B1, "4")}
        dropped = self.branch()
        result = build_strata(dropped, freeze=True)
        self.assertNotIn("S3", strata_of(dropped))
        self.assertEqual("S3", result["dropped"][0]["stratum"])
        with closing(dropped.connect()) as connection:
            sizes = connection.execute("SELECT eligible, selected FROM fu_stratum_sizes WHERE stratum='S3' AND "
                                       "journal='*'").fetchone()
            self.assertEqual((2, 0), tuple(sizes))
            frozen = json.loads(require_freeze(connection)["sizes_json"])
        self.assertEqual("S3", frozen["dropped"][0]["stratum"])
        verified = self.branch(protocol_with(S3={"licensingVerified": True}))
        build_strata(verified)
        s3 = strata_of(verified)["S3"]
        self.assertEqual(family, set(s3))
        self.assertTrue(all(row["inclusion_probability"] == 1.0 for row in s3.values()))

    def test_sizes_per_stratum_and_journal(self):
        branch = self.branch()
        build_strata(branch)
        with closing(branch.connect()) as connection:
            sizes = {(row[0], row[1]): tuple(row[2:]) for row in connection.execute(
                "SELECT stratum, journal, eligible, selected, pilot FROM fu_stratum_sizes")}
        journal_of = {lid(citation[0], citation[1], citation[2]): citation[0] for citation in F.CITATIONS}
        pilots = {journal: sum(1 for unit, row in strata_of(branch)["S1"].items()
                               if row["pilot"] and journal_of[unit] == journal)
                  for journal in (F.JOURNAL_A, F.JOURNAL_B)}
        self.assertEqual((3, 3, pilots[F.JOURNAL_A]), sizes[("S1", F.JOURNAL_A)])
        self.assertEqual((2, 2, pilots[F.JOURNAL_B]), sizes[("S1", F.JOURNAL_B)])
        self.assertEqual((5, 5, 4), sizes[("S1", "*")])
        self.assertEqual((4, 2, 1), sizes[("S0", "*")])
        self.assertEqual((2, 2, 0), sizes[("co-cited", "*")])

    def test_freeze_digest_is_stable_and_the_strata_cannot_be_built_again(self):
        first, second = self.branch(), self.branch()
        one = build_strata(first, freeze=True)["freeze"]
        two = build_strata(second, freeze=True)["freeze"]
        self.assertEqual(one["strataSha256"], two["strataSha256"])
        self.assertEqual(one["freezeId"], two["freezeId"])
        with closing(first.connect()) as connection:
            row = require_freeze(connection)
            self.assertEqual(one["strataSha256"], followup_strata.strata_digest(connection))
            self.assertEqual("snapshot-1", row["status_snapshot"])
            self.assertEqual(F.PROTOCOL["estimands"], json.loads(row["estimands_json"]))
            self.assertEqual(first.meta()["parentSha256"], row["parent_sha256"])
            self.assertEqual(first.meta()["protocolSha256"], row["protocol_sha256"])
        with self.assertRaisesRegex(ValidationError, "frozen"):
            followup_strata.cmd_strata(SimpleNamespace(database=str(first.path), freeze=False))
        # A freeze asked again (its step sent again after the result was lost) reports the branch's freeze unchanged.
        again = followup_strata.cmd_strata(SimpleNamespace(database=str(first.path), freeze=True))
        self.assertTrue(again["alreadyFrozen"])
        self.assertEqual({key: one[key] for key in ("freezeId", "strataSha256", "protocolSha256", "frozenAt")},
                         {key: again["freeze"][key] for key in ("freezeId", "strataSha256", "protocolSha256",
                                                                "frozenAt")})
        with closing(first.connect()) as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM fu_protocol_freeze").fetchone()[0])
            self.assertEqual(one["strataSha256"], followup_strata.strata_digest(connection))
        with self.assertRaisesRegex(ValidationError, "frozen"):
            followup_stats.cmd_hotspots(SimpleNamespace(database=str(first.path)))
        other = self.branch(protocol_with(S0={"size": 3, "matchOn": ["journal", "year"]}))
        self.assertNotEqual(one["strataSha256"], build_strata(other, freeze=True)["freeze"]["strataSha256"])

    def test_building_needs_status_rows_and_the_hotspot_test(self):
        missing = self.branch(statuses=None)
        with self.assertRaisesRegex(ValidationError, "status row"):
            build_strata(missing)
        partial = self.branch(statuses=None)
        insert_statuses(partial, skip=("w-plain-2",))
        with self.assertRaisesRegex(ValidationError, "1 cited works"):
            build_strata(partial)
        no_hotspots = self.branch()
        with self.assertRaisesRegex(ValidationError, "hotspot"):
            followup_strata.cmd_strata(SimpleNamespace(database=str(no_hotspots.path), freeze=False))

    def test_a_branch_without_a_protocol_is_refused(self):
        path = self.root / "no-protocol.sqlite3"
        FollowupBranch.create(path, self.census["parent"])
        with self.assertRaisesRegex(ValidationError, "no protocol"):
            followup_strata.cmd_strata(SimpleNamespace(database=str(path), freeze=False))

    def test_seeded_selection_and_the_replicate_share(self):
        keys = [f"unit-{index}" for index in range(40)]
        self.assertEqual(followup_strata.seeded_sample(5, ("x",), keys, 7),
                         followup_strata.seeded_sample(5, ("x",), list(reversed(keys)), 7))
        self.assertNotEqual(followup_strata.seeded_sample(5, ("x",), keys, 7),
                            followup_strata.seeded_sample(6, ("x",), keys, 7))
        self.assertNotEqual(followup_strata.seeded_sample(5, ("x",), keys, 7),
                            followup_strata.seeded_sample(5, ("y",), keys, 7))
        protocol = F.protocol()
        chosen = followup_strata.replicate_keys(protocol, keys)
        self.assertEqual(20, len(chosen))
        self.assertEqual(chosen, followup_strata.replicate_keys(protocol, keys))
        self.assertEqual([key for key in keys if key in chosen], chosen)
        self.assertNotEqual(chosen, followup_strata.replicate_keys(protocol, keys, scope_parts=("numeric-check@1",)))
        tenth = followup_strata.replicate_keys(protocol_with(replicate={"share": 0.1}), keys[:30])
        self.assertEqual(3, len(tenth))
        no_share = F.protocol()
        del no_share["strata"]["replicate"]
        with self.assertRaisesRegex(ValidationError, "replicate"):
            followup_strata.replicate_keys(no_share, keys)
        # Stratified: every replicate group gets the ceiling of its share, so a small stratum or status is never
        # left without replicate units.
        groups = {key: ("S1:retracted" if index < 2 else "S0") for index, key in enumerate(keys)}
        stratified = followup_strata.replicate_keys(protocol_with(replicate={"share": 0.1}), keys, groups=groups)
        self.assertEqual({"S1:retracted": 1, "S0": 4}, {group: sum(1 for key in stratified if groups[key] == group)
                                                       for group in ("S1:retracted", "S0")})
        self.assertEqual("S1:expression-of-concern",
                         followup_strata.replicate_group({"co-cited", "S1"}, "expression-of-concern"))
        self.assertEqual("S2", followup_strata.replicate_group({"co-cited", "S2", "S0"}, None))
        self.assertEqual("co-cited", followup_strata.replicate_group({"co-cited"}, "none"))

    def test_family_rules_must_be_defined_and_valid(self):
        protocol = protocol_with(S2={"excludeFamilies": ["registry-report"]})
        with self.assertRaisesRegex(ValidationError, "registry-report"):
            followup_strata.excluded_patterns(protocol)
        protocol = protocol_with(S3={"familyRules": {"version": "r", "titlePatterns": ["(unclosed"]}})
        with self.assertRaisesRegex(ValidationError, "regular expression"):
            followup_strata.s3_family(protocol)
        self.assertTrue(followup_strata.title_matches(followup_strata.s3_family(F.protocol()),
                                                      "Synthetic disease  Statistics\nReport, second edition"))

    def test_the_command_line_builds_and_freezes(self):
        branch = self.branch()
        followup_stats.cmd_hotspots(SimpleNamespace(database=str(branch.path)))
        from ocsl_review.cli import main
        target = self.root / "strata-result.json"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["--result-file", str(target), "followup", "strata", "--database", str(branch.path),
                         "--freeze"])
        record = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(0, code, record)
        self.assertEqual(["followup", "strata"], record["command"])
        self.assertRegex(record["result"]["freeze"]["strataSha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
