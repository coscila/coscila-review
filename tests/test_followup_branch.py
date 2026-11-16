# SPDX-License-Identifier: Apache-2.0
"""The follow-up branch: a delta database over a frozen census, parent read-only and unchanged."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import followup_fixtures as F
from ocsl_review import followup_schema, scores_db
from ocsl_review.errors import ValidationError
from ocsl_review.followup_branch import (FollowupBranch, read_artifact, register_dataset, require_freeze,
                                         store_artifact)
from ocsl_review.followup_model import claim_id, locus_id, sentence_digest


class BranchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.census = F.make_parent(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def branch(self, name: str, **options) -> FollowupBranch:
        path = self.root / name
        FollowupBranch.create(path, self.census["parent"], **options)
        return FollowupBranch(path)

    def test_create_records_the_parent_and_leaves_it_unchanged(self):
        before = scores_db.file_sha256(self.census["parent"])
        result = FollowupBranch.create(self.root / "create.sqlite3", self.census["parent"],
                                       parent_text=self.census["text"], protocol=F.protocol())
        self.assertEqual(before, result["parent"]["sha256"])
        self.assertEqual(before, scores_db.file_sha256(self.census["parent"]))
        self.assertEqual({"reviews": 3, "citations": len(F.CITATIONS), "loci": len(F.CITATIONS), "works": len(F.WORKS)},
                         result["parent"]["counts"])
        self.assertRegex(result["protocolSha256"], r"^[0-9a-f]{64}$")
        branch = FollowupBranch(self.root / "create.sqlite3")
        report = branch.verify()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(before, report["parent"]["parentSha256"])
        self.assertEqual(len(F.WORKS), report["citedWorksWithoutStatusRow"])
        kinds = {row["kind"] for row in branch.describe()["datasets"]}
        self.assertEqual({"parent", "parent-text", "protocol"}, kinds)
        self.assertEqual(F.PROTOCOL["seed"], branch.protocol()["seed"])
        self.assertFalse(stat.S_IMODE(self.census["parent"].stat().st_mode) & 0o222)

    def test_the_parent_is_attached_read_only_and_never_copied(self):
        branch = self.branch("attached.sqlite3")
        with closing(branch.connect(write=True)) as connection:
            self.assertEqual(len(F.CITATIONS), connection.execute("SELECT COUNT(*) FROM census.citations").fetchone()[0])
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("INSERT INTO census.meta VALUES('x', 'y')")
            self.assertNotIn("citations", followup_schema.tables(connection))
        self.assertLess(branch.path.stat().st_size, self.census["parent"].stat().st_size * 4)

    def test_redacted_sentences_come_only_from_a_matching_text_parent(self):
        plain = self.branch("redacted.sqlite3")
        with closing(plain.connect()) as connection:
            text, reason = plain.sentence(connection, F.JOURNAL_A, "10.5555/fu.review.a1", "1", 1)
            self.assertIn("⟨R1⟩", text)
            self.assertIsNone(reason)
            self.assertEqual((None, "text-redacted"), plain.sentence(connection, F.JOURNAL_A, "10.5555/fu.review.a2",
                                                                     "2", 1))
            self.assertEqual((None, "no-citing-text"), plain.sentence(connection, F.JOURNAL_A, "10.5555/fu.review.a2",
                                                                      "9", 1))
        with_text = self.branch("text.sqlite3", parent_text=self.census["text"])
        with closing(with_text.connect()) as connection:
            text, reason = with_text.sentence(connection, F.JOURNAL_A, "10.5555/fu.review.a2", "2", 1)
            self.assertTrue(text.startswith("A retracted cohort study"))
            redacted = connection.execute("SELECT sentence FROM census.loci WHERE doi='10.5555/fu.review.a2' AND label='2'"
                                          ).fetchone()[0]
            self.assertEqual(sentence_digest(redacted), hashlib.sha256(text.encode()).hexdigest())

    def test_steps_without_the_text_parent_run_and_steps_that_read_sentences_refuse(self):
        """The private keep-text copy is mounted only for steps that read citing sentences: without it a branch
        connects, verifies, resolves status, builds strata and exports publicly, while support, function, numeric,
        second-gen candidates and a private export refuse instead of turning entitled loci into text-redacted."""
        import shutil
        from types import SimpleNamespace
        from ocsl_review import followup_exports, followup_function
        text_copy = self.root / "census-text-copy.sqlite3"
        shutil.copy2(self.census["text"], text_copy)
        branch = self.branch("unmounted.sqlite3", parent_text=text_copy, protocol=F.protocol())
        hidden = text_copy.with_suffix(".hidden")
        text_copy.rename(hidden)
        try:
            self.assertFalse(branch.text_mounted)
            report = branch.verify()
            self.assertTrue(report["ok"], report["problems"])
            self.assertFalse(report["parent"]["parentTextMounted"])
            self.assertTrue(report["notes"])
            with closing(branch.connect()) as connection:
                attached = [row[1] for row in connection.execute("PRAGMA database_list")]
                self.assertEqual(["main", "census"], attached)
                self.assertIn("⟨R1⟩", branch.sentence(connection, F.JOURNAL_A, "10.5555/fu.review.a1", "1", 1)[0])
                with self.assertRaisesRegex(ValidationError, "not mounted"):
                    branch.sentence(connection, F.JOURNAL_A, "10.5555/fu.review.a2", "2", 1)
            with self.assertRaisesRegex(ValidationError, "not mounted"):
                branch.require_text()
            with branch.writing() as connection:
                connection.execute("INSERT INTO fu_protocol_freeze VALUES('freeze-x', ?, ?, 's', ?, '[]', '{}', "
                                   "'2026-01-01T00:00:00.000000Z')", ("a" * 64, "b" * 64, "c" * 64))
                with self.assertRaisesRegex(ValidationError, "not mounted"):
                    followup_function.run_function(connection, branch, F.ScriptedJudge(lambda m, s: "{}"), None,
                                                   model=F.MODEL)
            exported = followup_exports.export(branch.path, self.root / "unmounted-export")
            self.assertFalse(exported["private"])
            with self.assertRaisesRegex(ValidationError, "not mounted"):
                followup_exports.export(branch.path, self.root / "unmounted-private", allow_entitled_text=True)
        finally:
            hidden.rename(text_copy)
        self.assertTrue(branch.text_mounted)
        with closing(branch.connect()) as connection:
            self.assertTrue(branch.sentence(connection, F.JOURNAL_A, "10.5555/fu.review.a2", "2", 1)[0])

    def test_an_init_asked_again_with_its_inputs_reports_the_branch_it_made(self):
        # A step sent again after its result was lost: the same inputs report the branch unchanged; any other input,
        # and a branch made without a text parent, is still refused.
        path = self.root / "again.sqlite3"
        bundle = "c" * 64
        first = FollowupBranch.create(path, self.census["parent"], parent_text=self.census["text"],
                                      protocol=F.protocol(), study_bundle=bundle)
        before = scores_db.file_sha256(path)
        again = FollowupBranch.create(path, self.census["parent"], parent_text=self.census["text"],
                                      protocol=F.protocol(), study_bundle=bundle)
        self.assertTrue(again["alreadyMade"])
        self.assertEqual((first["database"], first["protocolSha256"], bundle),
                         (again["database"], again["protocolSha256"], again["studyBundle"]["studyBundleSha256"]))
        self.assertEqual(before, scores_db.file_sha256(path), "the branch is left as it was")
        for changed in ({"parent_text": None}, {"protocol": F.protocol(seed=12)}, {"study_bundle": "d" * 64},
                        {"study_bundle": None}):
            options = {"parent_text": self.census["text"], "protocol": F.protocol(), "study_bundle": bundle, **changed}
            with self.subTest(changed=sorted(changed)), self.assertRaisesRegex(ValidationError, "already exists"):
                FollowupBranch.create(path, self.census["parent"], **options)

    def test_an_init_killed_before_its_commit_leaves_nothing_that_blocks_the_next(self):
        path = self.root / "killed.sqlite3"
        with closing(sqlite3.connect(path)) as connection:   # an SQLite file without any schema object
            connection.execute("PRAGMA user_version=0")
        self.assertTrue(path.is_file())
        result = FollowupBranch.create(path, self.census["parent"], protocol=F.protocol())
        self.assertNotIn("alreadyMade", result)
        self.assertTrue(FollowupBranch(path).verify()["ok"])
        unrelated = self.root / "unrelated.sqlite3"
        with closing(sqlite3.connect(unrelated)) as connection:
            connection.execute("CREATE TABLE notes(text TEXT)")
            connection.commit()
        with self.assertRaisesRegex(ValidationError, "already exists"):
            FollowupBranch.create(unrelated, self.census["parent"])

    def test_create_refuses_unusable_parents(self):
        with self.assertRaisesRegex(ValidationError, "frozen"):
            FollowupBranch.create(self.root / "working.sqlite3", self.census["working"])
        with self.assertRaisesRegex(ValidationError, "absolute"):
            FollowupBranch.create(self.root / "relative.sqlite3", Path("census.sqlite3"))
        with self.assertRaisesRegex(ValidationError, "keeps its text"):
            FollowupBranch.create(self.root / "notext.sqlite3", self.census["parent"], parent_text=self.census["parent"])
        self.branch("exists.sqlite3")
        with self.assertRaisesRegex(ValidationError, "already exists"):
            FollowupBranch.create(self.root / "exists.sqlite3", self.census["parent"],
                                  protocol=F.protocol(seed=12))
        with self.assertRaisesRegex(ValidationError, "protocol"):
            FollowupBranch.create(self.root / "badprotocol.sqlite3", self.census["parent"],
                                  protocol=F.protocol(seed="eleven"))
        other = self.root / "other"
        other.mkdir()
        working = F.make_working_census(other / "working.sqlite3")
        with closing(scores_db.connect(working)) as connection:
            connection.execute("UPDATE reviews SET provenance_class='public' WHERE doi='10.5555/fu.review.a2'")
            connection.commit()
        scores_db.freeze(working, other / "text.sqlite3", keep_text=True)
        with self.assertRaisesRegex(ValidationError, "population"):
            FollowupBranch.create(self.root / "mismatch.sqlite3", self.census["parent"], parent_text=other / "text.sqlite3")

    def test_verify_finds_dangling_references_and_a_changed_parent(self):
        copy_root = self.root / "changed"
        copy_root.mkdir()
        census = F.make_parent(copy_root)
        FollowupBranch.create(copy_root / "branch.sqlite3", census["parent"])
        branch = FollowupBranch(copy_root / "branch.sqlite3")
        with branch.writing() as connection:
            connection.execute("INSERT INTO fu_frame(locus_id, journal, doi, label, seq, work_key, claim_id, "
                               "sentence_sha256, unit, provenance_class, identity, cited_inbody) "
                               "VALUES('locus-x', ?, '10.5555/none', '1', 1, 'w-none', 'claim-x', ?, 1, 'public', "
                               "'resolved', 1)", (F.JOURNAL_A, "0" * 64))
        report = branch.verify()
        self.assertFalse(report["ok"])
        self.assertEqual(1, report["references"]["frameLociMissingInParent"])
        self.assertEqual(1, report["references"]["frameWorksMissingInParent"])
        os.chmod(census["parent"], 0o644)
        with closing(sqlite3.connect(census["parent"])) as connection:
            connection.execute("INSERT INTO meta VALUES('tampered', '1')")
            connection.commit()
        with self.assertRaisesRegex(ValidationError, "changed since the branch was made"):
            branch.connect()
        problems = branch.verify()["problems"]
        self.assertTrue(any("digest changed" in problem for problem in problems))

    def test_schema_check_dictionary_and_append_only_ledgers(self):
        branch = self.branch("schema.sqlite3")
        with closing(branch.connect(write=True)) as connection:
            self.assertEqual([], followup_schema.check(connection))
            columns = {(r[0], r[1]) for r in connection.execute("SELECT table_name, column_name FROM fu_dictionary")}
            self.assertIn(("fu_status_observations", "covers_json"), columns)
            sha = store_artifact(connection, b"request bytes", "judge-request", "application/json")
            self.assertEqual(b"request bytes", read_artifact(connection, sha))
            dataset = register_dataset(connection, kind="status-snapshot", name="status", declared_version="t1",
                                       sha256="a" * 64)
            self.assertEqual(dataset, register_dataset(connection, kind="status-snapshot", name="status",
                                                       declared_version="t1", sha256="a" * 64))
            with self.assertRaisesRegex(ValidationError, "other content"):
                register_dataset(connection, kind="table-source", name="status", declared_version="t1",
                                 sha256="a" * 64)
            for statement in ("UPDATE fu_artifacts SET kind='x'", "DELETE FROM fu_datasets"):
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    connection.execute(statement)
            with self.assertRaisesRegex(ValidationError, "not frozen"):
                require_freeze(connection)
            connection.execute("CREATE TABLE stray(x)")
            self.assertTrue(any("outside the follow-up schema" in p for p in followup_schema.check(connection)))
            connection.rollback()

    def test_unit_checks_reject_inconsistent_verdict_rows(self):
        branch = self.branch("checks.sqlite3")
        with closing(branch.connect(write=True)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO fu_support(locus_id, tier, verdict, assessable, reason) "
                                   "VALUES('locus-a', 'full-text', NULL, 1, NULL)")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO fu_support(locus_id, tier, verdict, assessable, reason) "
                                   "VALUES('locus-a', 'abstract', 'abstain', 1, NULL)")
            connection.execute("INSERT INTO fu_support(locus_id, tier, verdict, assessable, reason) "
                               "VALUES('locus-a', 'abstract', 'abstain', 0, 'abstained')")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO fu_function(locus_id, class, assessable, reason) "
                                   "VALUES('locus-b', 'relies', 0, 'abstained')")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO fu_work_status(work_key, status, sources_json, checked_at, snapshot_id, "
                                   "rule_version, resolved_at) VALUES('w', 'reinstated', '[]', 't', 's', 'r', 't')")
            connection.rollback()

    def test_identifiers_are_stable(self):
        self.assertEqual(locus_id("J", "10.1/x", "3", 2), locus_id("J", "10.1/x", "3", 2))
        self.assertNotEqual(locus_id("J", "10.1/x", "3", 2), locus_id("J", "10.1/x", "3", 3))
        digest = hashlib.sha256(b"text").hexdigest()
        self.assertEqual(digest, sentence_digest("text"))
        self.assertEqual(digest, sentence_digest("sha256:" + digest))
        self.assertTrue(claim_id("J", "10.1/x", 4, digest).startswith("claim-"))

    def test_migrate_refuses_a_newer_branch(self):
        branch = self.branch("migrate.sqlite3")
        with closing(branch.connect(write=True)) as connection:
            self.assertEqual(followup_schema.SCHEMA_VERSION, followup_schema.migrate(connection))
            connection.execute("UPDATE fu_meta SET value='99' WHERE key='schemaVersion'")
            with self.assertRaisesRegex(ValidationError, "newer"):
                followup_schema.migrate(connection)
            connection.rollback()


if __name__ == "__main__":
    unittest.main()
