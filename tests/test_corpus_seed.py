# SPDX-License-Identifier: Apache-2.0
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ocsl_review.corpus_seed import archive_file, import_legacy_evidence, import_legacy_fulltext_cache
from ocsl_review.corpus_store import CorpusRepository
from ocsl_review.errors import ValidationError


class CorpusSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.legacy = self.root / "historical"
        (self.legacy / "work/reviews").mkdir(parents=True)
        self.repo = CorpusRepository.create(self.root, "work/library", "Reusable evidence", "seed-v2", {"kind": "seed"})

    def database(self):
        path = self.legacy / "work/reviews/nrco_citation_db.sqlite3"
        db = sqlite3.connect(path)
        db.executescript('''CREATE TABLE works(work_key TEXT,doi TEXT,title TEXT,year INT,abstract TEXT,abstract_source TEXT,local_path TEXT,origin_url TEXT,fulltext_format TEXT);
          CREATE TABLE papers(doi TEXT,title TEXT,year INT,path_xml TEXT);
          CREATE TABLE bib_refs(doi TEXT,label TEXT,printed TEXT,work_key TEXT);
          CREATE TABLE citations(doi TEXT,label TEXT,sentence TEXT);
          INSERT INTO works VALUES('doi:10.1234/one','10.1234/one','First paper',2024,'Legacy field one.','europepmc',NULL,NULL,NULL);
          INSERT INTO works VALUES('doi:10.1234/two','10.1234/two','Second paper',2024,'Legacy field two.','semanticscholar',NULL,NULL,NULL);
          INSERT INTO papers VALUES('10.1234/review','Review',2024,'work/corpus/review.xml');
          INSERT INTO bib_refs VALUES('10.1234/review','1','First paper (2024).','doi:10.1234/one');
          INSERT INTO citations VALUES('10.1234/review','1','Legacy sentence with known parsing limitations.');''')
        db.commit(); db.close()
        (self.legacy / "work/corpus").mkdir()
        (self.legacy / "work/corpus/review.xml").write_bytes(b'<article><body><p>Original paper text.</p></body></article>')
        return path

    def test_sharded_abstracts_keep_identity_and_unverified_provenance_separate(self):
        path = self.database()
        before = {p: p.read_bytes() for p in self.legacy.rglob('*') if p.is_file()}
        result = import_legacy_evidence(self.repo, path)
        self.assertEqual(result, import_legacy_evidence(self.repo, path))
        self.assertEqual(2, result["works"])
        self.assertEqual(2, result["legacyAbstracts"])
        self.assertEqual(1, result["legacyReferenceRows"])
        self.assertEqual(1, result["legacyLocusRows"])
        with self.repo.connect() as db:
            works = {json.loads(r[1])["doi"]: r[0] for r in db.execute('SELECT work_id,metadata_json FROM works')}
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM assessments').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM claim_loci').fetchone()[0])
        first = self.repo.evidence(works['10.1234/one'], 'abstract')
        second = self.repo.evidence(works['10.1234/two'], 'abstract')
        self.assertEqual(['Legacy field one.'], [u['text'] for u in first['evidenceUnits']])
        self.assertEqual(['Legacy field two.'], [u['text'] for u in second['evidenceUnits']])
        self.assertFalse(first['complete']); self.assertFalse(second['complete'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.legacy.rglob('*') if p.is_file()})
        self.assertEqual('seed-v2', self.repo.analysis()['analysis_id'])
        self.repo.library.verify()

    def test_only_actual_structured_provider_cache_can_certify_complete_abstract(self):
        path = self.database()
        rawdir = self.legacy / 'work/raw/epmc_abs'
        rawdir.mkdir(parents=True)
        payload = {'request': {'queryString': 'DOI:10.1234/one', 'resultType': 'core', 'pageSize': 1},
                   'resultList': {'result': [{'id': '123', 'source': 'MED', 'doi': '10.1234/one', 'title': 'First paper', 'pubYear': '2024', 'abstractText': 'Complete actual provider field.'}]}}
        raw = json.dumps(payload).encode()
        (rawdir / 'batch.json').write_bytes(raw)
        result = import_legacy_evidence(self.repo, path)
        self.assertEqual(1, result['completeProviderAbstracts'])
        work = self.repo.work({'doi': '10.1234/one'})
        evidence = self.repo.evidence(work, 'abstract')
        self.assertTrue(evidence['complete'])
        self.assertEqual(['Complete actual provider field.'], [u['text'] for u in evidence['evidenceUnits']])
        self.assertEqual(raw, self.repo.read_artifact(hashlib.sha256(raw).hexdigest()))

    def test_local_path_cannot_archive_credentials_and_missing_files_remain_explicit(self):
        path = self.database()
        secret = self.legacy / 'work/audit/.env'
        secret.parent.mkdir(); secret.write_bytes(b'private-do-not-import')
        db = sqlite3.connect(path)
        db.execute('UPDATE works SET local_path=? WHERE doi=?', ('work/audit/.env','10.1234/one'))
        db.commit(); db.close()
        result = import_legacy_evidence(self.repo, path)
        sources = json.loads(self.repo.read_artifact(result['sourceManifestSha256']))
        self.assertTrue(any(item['state']=='missing-or-outside-source-allowlist' for item in sources))
        with self.repo.connect() as db:
            self.assertIsNone(db.execute('SELECT sha256 FROM source_artifacts WHERE sha256=?', (hashlib.sha256(secret.read_bytes()).hexdigest(),)).fetchone())

    def test_file_publication_recovers_after_interrupted_registry_insert(self):
        body = b'Archived before registry transaction committed.'
        source = self.legacy / 'input.txt'; source.write_bytes(body)
        sha = hashlib.sha256(body).hexdigest()
        (self.repo.root / 'sources' / sha).write_bytes(body)
        self.assertEqual(sha, archive_file(self.repo, source, 'fixture'))
        self.assertEqual(body, self.repo.read_artifact(sha))

    def test_original_xml_accepts_optional_pmc_prefix_but_rejects_identifier_conflicts(self):
        path = self.database()
        directory = self.legacy / 'work/library'; directory.mkdir()
        db = sqlite3.connect(path)
        db.execute('ALTER TABLE works ADD COLUMN pmcid TEXT')
        for slug, recorded, observed in [('one', '123', 'PMC123'), ('two', '999', 'PMC456')]:
            name = 'work/library/' + slug + '.xml'
            (self.legacy / name).write_text('<article><front><article-meta>'
                '<article-id pub-id-type="doi">10.1234/' + slug + '</article-id>'
                '<article-id pub-id-type="pmc">' + observed + '</article-id>'
                '</article-meta></front><body><p>Original complete source text.</p></body></article>')
            db.execute('UPDATE works SET pmcid=?,local_path=? WHERE doi=?',
                       (recorded, name, '10.1234/' + slug))
        db.commit(); db.close()
        import_legacy_evidence(self.repo, path)
        self.assertTrue(self.repo.evidence(self.repo.work({'doi': '10.1234/one'}), 'full-text')['complete'])
        self.assertFalse(self.repo.evidence(self.repo.work({'doi': '10.1234/two'}), 'full-text')['complete'])

    def test_audited_text_exports_remain_explicitly_incomplete(self):
        directory = self.legacy / 'work/audit/panel'; directory.mkdir(parents=True)
        cachedir = self.legacy / 'work/audit/library'; cachedir.mkdir()
        cited = {'doi': '10.1234/one', 'pmcid': 'PMC123', 'title': 'First paper'}
        name = hashlib.sha256(b'10.1234/one|PMC123').hexdigest()[:24] + '.txt'
        (cachedir / name).write_text('Archived extracted source text.')
        (directory / 'manifest_test.ft.jsonl').write_text(json.dumps({'cited_work': cited, 'fulltext_source': 'cache', 'fulltext': 'A smaller old selected window.'}) + '\n')
        result = import_legacy_fulltext_cache(self.repo, self.legacy)
        self.assertEqual(1, result['distinctDocuments'])
        evidence = self.repo.evidence(self.repo.work(cited), 'full-text')
        self.assertFalse(evidence['complete'])
        self.assertEqual(['Archived extracted source text.'], [unit['text'] for unit in evidence['evidenceUnits']])

    def test_hash_mismatch_in_audit_source_prevents_completion(self):
        path = self.legacy / 'work/reviews/audit.sqlite3'
        xml = self.legacy / 'work/corpus/review.xml'; xml.parent.mkdir(); xml.write_bytes(b'changed')
        db = sqlite3.connect(path)
        db.executescript('''CREATE TABLE abstracts_epmc(work_key TEXT,doi TEXT,pmid TEXT,title TEXT,pub_year TEXT,abstract TEXT,source TEXT);
            CREATE TABLE paper_parse(doi TEXT,paper_key TEXT,path TEXT,file_sha256 TEXT);
            CREATE TABLE refs_corrected(doi TEXT,label TEXT,printed TEXT);''')
        db.execute('INSERT INTO paper_parse VALUES(?,?,?,?)', ('10.1234/review','review','work/corpus/review.xml','a'*64)); db.commit(); db.close()
        with self.assertRaisesRegex(ValidationError, 'hash mismatch'):
            import_legacy_evidence(self.repo, path)
        with self.repo.connect() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM corpus_imports WHERE source_kind='evidence'").fetchone()[0])


if __name__ == '__main__':
    unittest.main()
