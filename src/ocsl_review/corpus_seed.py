# SPDX-License-Identifier: Apache-2.0
"""Independently implemented adapters for archived literature evidence.

Old provenance labels are retained, not upgraded into completeness attestations.
Only retained structured provider fields or identity-matched original documents
can establish complete evidence. Scientific judgments remain legacy observations.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import urlencode

from .corpus_import import _completed, _finish, _namespace, _observation, _observations, _regular, _stat
from .corpus_store import CorpusRepository, canonical, identity
from .corpus_library import now, digest_file
from .errors import ProviderError, ReviewError, ValidationError
from .evidence import abstract_candidate, complete_abstract
from .fulltext import parse_source_document
from .scholarly import EUROPE_PMC, normalize_doi, normalize_europe_pmc


VERSION = "legacy-evidence-v1"


def archive_file(repo: CorpusRepository, source: Path, kind: str, media_type="application/octet-stream") -> str:
    """Stream a stable, explicit file into the portable content-addressed store."""
    if (repo.root / "manifest.json").exists():
        raise ValidationError("immutable snapshots cannot be modified; branch first")
    source = _regular(Path(source))
    before = _stat(source)
    digest = hashlib.sha256()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=repo.root / "sources", delete=False) as target:
            temporary = Path(target.name)
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as incoming:
                for block in iter(lambda: incoming.read(1024 * 1024), b""):
                    digest.update(block); target.write(block)
            target.flush(); os.fsync(target.fileno())
        if before != _stat(source):
            raise ValidationError("source changed during seed capture; pause its writer")
        sha = digest.hexdigest()
        temporary.chmod(0o444)
        destination = repo.root / "sources" / sha
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if digest_file(destination) != sha:
                raise ValidationError("stored source artifact hash mismatch")
        with repo.connect(write=True) as connection:
            connection.execute("INSERT OR IGNORE INTO source_artifacts VALUES(?,?,?,?,?,?)",
                (sha, "sources/" + sha, before[1], media_type, kind, now()))
        return sha
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _metadata(record):
    value = {key: record.get(key) for key in ("doi", "pmid", "pmcid", "title", "year", "venue", "authors") if record.get(key) not in (None, "")}
    if value.get("doi"):
        try:
            value["doi"] = normalize_doi(value["doi"])
        except ReviewError:
            value["legacyInvalidDoi"] = value.pop("doi")
    if value.get("pmcid"):
        pmcid = str(value["pmcid"]).strip().upper()
        digits = re.fullmatch(r"(?:PMC)?([0-9]+)", pmcid)
        value["pmcid"] = "PMC" + digits[1] if digits else pmcid
    return value


def _work_id(metadata, fallback):
    key = next((f"{k}:{str(metadata[k]).lower()}" for k in ("doi", "pmid", "pmcid") if metadata.get(k)), fallback)
    return "work-" + identity(key)


def _work(connection, record, fallback):
    metadata = _metadata(record)
    work_id = _work_id(metadata, fallback)
    connection.execute("INSERT OR IGNORE INTO works VALUES(?,?,?,?)", (work_id, metadata.get("title") or "Unresolved work", metadata.get("year"), canonical(metadata)))
    return work_id


def _document(connection, work, artifact, text, tier, provider, locator, complete=False, source_url="", metadata=None):
    document_id = "document-" + identity(work, artifact, tier)
    connection.execute("INSERT OR IGNORE INTO corpus_documents VALUES(?,?,?,?,?,?,?,?,?)",
        (document_id, work, artifact, tier, source_url, provider, int(complete), canonical(metadata or {}), now()))
    section_id = "section-" + identity(work, artifact, locator)
    connection.execute("INSERT OR IGNORE INTO source_sections VALUES(?,?,?,?,?,?,?,?,?)",
        (section_id, work, artifact, locator, tier, text, hashlib.sha256(text.encode()).hexdigest(), VERSION, int(complete)))


def _chunks(connection, table, size=1000):
    cursor = connection.execute(f'SELECT rowid AS legacy_rowid,* FROM "{table}"')
    while rows := cursor.fetchmany(size):
        yield [dict(row) for row in rows]


def _source_path(root: Path, value) -> Path | None:
    if not value:
        return None
    candidate = Path(str(value))
    path = candidate if candidate.is_absolute() else root / candidate
    # An imported record is data, never permission to read arbitrary local files.
    try:
        relative = path.absolute().relative_to(root)
    except ValueError:
        return None
    allowed = ("work/frontiers/jats/", "work/corpus/", "work/raw/nature/", "work/library/",
               "work/audit/library/", "work/audit/nrco_v2/pages/", "work/audit/nrco_v2/tables/")
    if ".." in relative.parts or not str(relative).startswith(allowed) or path.suffix.lower() not in {".xml", ".html", ".pdf", ".txt", ".json"}:
        return None
    return _regular(path) if path.exists() else None


def _keep_source(repo, root, value, expected=None):
    path = _source_path(root, value)
    if path is None:
        return {"originalPath": str(value), "state": "missing-or-outside-source-allowlist"}
    media = {".xml": "application/xml", ".html": "text/html", ".pdf": "application/pdf", ".txt": "text/plain", ".json": "application/json"}[path.suffix.lower()]
    sha = archive_file(repo, path, "legacy-original-source", media)
    if expected and sha != expected:
        raise ValidationError("registered historical source hash mismatch: " + str(path))
    return {"originalPath": str(path.relative_to(root)), "sha256": sha, "mediaType": media, "state": "retained"}


def _normalized_abstracts(repo, source, table, db_sha, mapping, progress):
    count = abstracts = 0
    for rows in _chunks(source, table):
        shard = repo.json_artifact({"sourceDatabaseSha256": db_sha, "table": table, "records": rows}, "legacy-evidence-shard")
        with repo.connect(write=True) as connection:
            for record in rows:
                if table == "abstracts_epmc":
                    record = {**record, "year": record.get("pub_year"), "abstract_source": record.get("source")}
                work = _work(connection, record, "legacy-work-" + identity(db_sha, record.get("work_key")))
                mapping[record.get("work_key")] = work
                text = record.get("abstract")
                if isinstance(text, str) and text.strip():
                    _document(connection, work, shard, text, "abstract", "legacy:" + str(record.get("abstract_source") or "unknown"),
                        canonical({"table": table, "rowid": record["legacy_rowid"], "column": "abstract"}),
                        metadata={"sourceDatabaseSha256": db_sha, "legacyProviderClaim": record.get("abstract_source"),
                                  "completenessBasis": "legacy-field-only-unverified"})
                    abstracts += 1
                count += 1
        if progress:
            progress({"phase": "legacy-abstracts", "records": count, "abstracts": abstracts})
    return count, abstracts


def _raw_core(repo, root, progress):
    """Only a narrow, known cache directory; never sweep arbitrary workspace JSON."""
    directory = root / "work/raw/epmc_abs"
    if not directory.is_dir():
        return {"rawProviderFiles": 0, "completeProviderAbstracts": 0}
    if directory.is_symlink():
        raise ValidationError("provider cache directory must not be a symlink")
    files = complete = 0
    for path in sorted(directory.glob("*.json")):
        sha = archive_file(repo, path, "legacy-provider-response", "application/json")
        payload = json.loads(repo.read_artifact(sha))
        request = payload.get("request", {})
        if request.get("resultType") != "core":
            continue
        url = f"{EUROPE_PMC}/search?" + urlencode({"query": request.get("queryString", ""), "resultType": "core", "format": "json", "pageSize": request.get("pageSize", 0)})
        # This is the import timestamp, explicitly not an invented original fetch time.
        imported_at = now()
        with repo.connect(write=True) as connection:
            for index, raw in enumerate(payload.get("resultList", {}).get("result", [])):
                record = normalize_europe_pmc(raw)
                if not record.get("abstract") or not any(record.get(k) for k in ("doi", "pmid", "pmcid")):
                    continue
                candidate = abstract_candidate(record, source_url=url, retrieved_at=imported_at)
                candidate["provenance"].update(retrievedAtBasis="import-of-retained-provider-response", originalRetrievedAt=None,
                                                sourceUrlBasis="reconstructed-from-retained-request")
                verified = complete_abstract(candidate)
                work = _work(connection, record, "legacy-raw-" + identity(sha, index))
                _document(connection, work, sha, record["abstract"], "abstract", "europe-pmc",
                    canonical({"jsonPointer": f"/resultList/result/{index}/abstractText"}), verified, url,
                    {"completeAbstractCandidate": candidate, "originalRetrievedAt": None, "importedAt": imported_at})
                complete += int(verified)
        files += 1
        if progress:
            progress({"phase": "legacy-provider-cache", "files": files, "completeAbstracts": complete})
    return {"rawProviderFiles": files, "completeProviderAbstracts": complete}


def import_legacy_evidence(repo: CorpusRepository, source_db: Path, *, source_root: Path | None = None,
                           progress=None) -> dict:
    """Import old evidence independently of active-study inclusion and verdicts."""
    source_db = _regular(Path(source_db))
    wal = Path(str(source_db) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValidationError("legacy evidence needs a quiescent SQLite snapshot without WAL")
    root = Path(source_root).absolute() if source_root else source_db.parents[2]
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValidationError("legacy source root must not use symlinks")
    before = _stat(source_db)
    db_sha = archive_file(repo, source_db, "legacy-evidence-database", "application/vnd.sqlite3")
    source = sqlite3.connect((repo.root / "sources" / db_sha).as_uri() + "?mode=ro&immutable=1", uri=True)
    source.row_factory = sqlite3.Row
    source.execute("PRAGMA trusted_schema=OFF")
    source.execute("PRAGMA query_only=ON")
    try:
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not ({"works", "papers", "bib_refs"} <= tables or {"abstracts_epmc", "paper_parse", "refs_corrected"} <= tables):
            raise ValidationError("unsupported legacy evidence database")
        manifest = {"snapshotSha256": db_sha, "originalDatabaseSha256": db_sha, "sourcePath": str(source_db)}
        analysis, method = _namespace(repo, "legacy-evidence", "evidence", manifest)
        if previous := _completed(repo, analysis):
            return previous
        summary = {"works": 0, "legacyAbstracts": 0, "legacyReferenceRows": 0,
                   "legacyLocusRows": 0, "reviewRepresentations": 0, "retainedSourceFiles": 0,
                   "rawProviderFiles": 0, "completeProviderAbstracts": 0}
        mapping = {}
        abstract_table = "works" if "works" in tables else "abstracts_epmc"
        summary["works"], summary["legacyAbstracts"] = _normalized_abstracts(repo, source, abstract_table, db_sha, mapping, progress)
        papers = "papers" if "papers" in tables else "paper_parse"
        review_works, source_manifest = {}, []
        for rows in _chunks(source, papers):
            records = []
            for record in rows:
                work = repo.work(_metadata(record), key=None if record.get("doi") else "legacy-paper-" + identity(db_sha, record.get("paper_key")))
                review_works[record.get("doi")] = work
                refs = []
                if papers == "papers":
                    for key in ("path_xml", "path_pdf", "path_text"):
                        if record.get(key):
                            refs.append(_keep_source(repo, root, record[key], record.get("sha256_pdf") if key == "path_pdf" else None))
                elif record.get("path"):
                    refs.append(_keep_source(repo, root, record["path"], record.get("file_sha256")))
                source_manifest.extend({"doi": record.get("doi"), "paperKey": record.get("paper_key"), **item} for item in refs)
                summary["retainedSourceFiles"] += sum(item["state"] == "retained" for item in refs)
                records.append(_observation(analysis, work, "legacy.review_manifest", str(record["legacy_rowid"]),
                    {"row": record, "sources": refs, "activeStudyInclusion": "not-inferred"}, method))
                summary["reviewRepresentations"] += 1
            _observations(repo, records)
            if progress:
                progress({"phase": "legacy-review-sources", "records": summary["reviewRepresentations"]})
        for table, property_name, counter in [("bib_refs", "legacy.bibliography", "legacyReferenceRows"),
                ("refs_corrected", "legacy.corrected_bibliography", "legacyReferenceRows"),
                ("citations", "legacy.citation_locus", "legacyLocusRows"),
                ("loci_corrected", "legacy.corrected_locus", "legacyLocusRows")]:
            if table not in tables:
                continue
            for rows in _chunks(source, table):
                records = []
                for record in rows:
                    doi = record.get("doi")
                    work = review_works.get(doi)
                    if work is None:
                        work = repo.work({"doi": doi}, key=None if doi else "legacy-review-" + identity(db_sha, record.get("paper_key")))
                        review_works[doi] = work
                    records.append(_observation(analysis, work, property_name, str(record["legacy_rowid"]),
                        {"sourceTable": table, "sourceDatabaseSha256": db_sha, "row": record,
                         "canonicalV2Binding": False}, method))
                _observations(repo, records)
                summary[counter] += len(rows)
                if progress:
                    progress({"phase": table, "records": summary[counter]})
        # Preserve the selected cited full-text source paths. Unmatched or legacy
        # text exports remain incomplete; original XML is parsed independently.
        if "works" in tables:
            for row in source.execute("SELECT * FROM works WHERE local_path IS NOT NULL AND local_path!=''"):
                record = dict(row)
                retained = _keep_source(repo, root, record["local_path"])
                source_manifest.append({"workKey": record["work_key"], **retained})
                if retained["state"] != "retained":
                    continue
                summary["retainedSourceFiles"] += 1
                path = _source_path(root, record["local_path"])
                try:
                    if path.suffix == ".pdf":
                        continue  # Preserve bytes; OCR/extraction is an explicit later operation.
                    document = parse_source_document(repo.read_artifact(retained["sha256"]), retained["mediaType"])
                    expected = _metadata(record)
                    observed = _metadata(document.get("identifiers", {}))
                    shared = [k for k in ("doi", "pmid", "pmcid") if expected.get(k) and observed.get(k)]
                    matched = bool(shared) and all(str(observed[k]).casefold() == str(expected[k]).casefold() for k in shared)
                    with repo.connect(write=True) as connection:
                        _document(connection, mapping[record["work_key"]], retained["sha256"], document["text"],
                            "full-text", "legacy-original-source", "independent full document extraction", bool(matched and document.get("complete")),
                            str(record.get("origin_url") or ""), {"legacyFormat": record.get("fulltext_format"), "identityMatched": matched})
                except (ProviderError, UnicodeError):
                    pass  # Raw artifact and explicit source state remain available.
        if "nrco_table_pages" in tables:
            for record in source.execute("SELECT path,url FROM nrco_table_pages WHERE path IS NOT NULL"):
                item = _keep_source(repo, root, record["path"])
                source_manifest.append({"sourceUrl": record["url"], **item})
                summary["retainedSourceFiles"] += int(item["state"] == "retained")
        # The old NRCO provider cache holds genuine core JSON; the Frontiers
        # abstract-only DB cannot manufacture these missing provider records.
        if source_db.name == "nrco_citation_db.sqlite3":
            summary.update(_raw_core(repo, root, progress))
        if "abstracts_epmc" in tables:
            summary["auditedFulltextCache"] = import_legacy_fulltext_cache(repo, root, progress=progress)
        summary["sourceManifestSha256"] = repo.json_artifact(source_manifest, "legacy-source-manifest")
        if before != _stat(source_db):
            raise ValidationError("legacy database changed during seed import")
        return _finish(repo, analysis, manifest, "evidence", summary)
    finally:
        source.close()


def import_legacy_fulltext_cache(repo: CorpusRepository, source_root: Path, *, progress=None) -> dict:
    """Preserve audited text exports as excerpts, including their exact old inputs.

    The archived manifests identify the cited work. Their text caches were made
    by an older extractor that flattened/capped content; they cannot certify a
    complete source even if the filename says fulltext.
    """
    root = Path(source_root).absolute()
    manifests = sorted((root / "work/audit/panel").glob("manifest_*.ft.jsonl"))
    archived, linked, count = set(), set(), 0
    metadata = []
    for path in manifests:
        manifest_sha = archive_file(repo, path, "legacy-fulltext-selection-manifest", "application/x-ndjson")
        before = _stat(path)
        with (repo.root / "sources" / manifest_sha).open() as stream:
            for line_number, line in enumerate(stream, 1):
                item = json.loads(line)
                cited = item.get("cited_work") or {}
                cache_key = hashlib.sha256(f"{cited.get('doi')}|{cited.get('pmcid')}".encode()).hexdigest()[:24]
                cached = root / "work/audit/library" / (cache_key + ".txt")
                if cached.is_file():
                    retained = _keep_source(repo, root, str(cached))
                    artifact = retained["sha256"]
                    text = repo.read_artifact(artifact).decode("utf-8")
                    archived.add(artifact)
                    locator = "legacy audited text export; original full-text completeness unverified"
                elif isinstance(item.get("fulltext"), str) and item["fulltext"].strip():
                    artifact, text = manifest_sha, item["fulltext"]
                    locator = canonical({"line": line_number, "field": "fulltext"})
                else:
                    continue
                with repo.connect(write=True) as connection:
                    work = _work(connection, cited, "legacy-cited-work-" + identity(manifest_sha, line_number))
                    if (work, artifact) not in linked:
                        _document(connection, work, artifact, text, "full-text", "legacy-audit-excerpt", locator,
                            False, "https://doi.org/" + cited["doi"] if cited.get("doi") else "",
                            {"selectionManifestSha256": manifest_sha, "selectionLine": line_number,
                             "legacySourceClaim": item.get("fulltext_source"), "completenessBasis": "legacy-extraction-unverified"})
                        linked.add((work, artifact))
                count += 1
                if progress and count % 100 == 0:
                    progress({"phase": "legacy-fulltext-cache", "records": count, "distinctDocuments": len(linked)})
        if before != _stat(path):
            raise ValidationError("legacy full-text selection manifest changed during import")
        metadata.append({"originalPath": str(path.relative_to(root)), "sha256": manifest_sha})
    # Retain the other scientific text-cache files too, even when no surviving
    # selected-item manifest proves their work mapping. Never infer that mapping.
    unbound = 0
    for path in sorted((root / "work/audit/library").glob("*.txt")):
        if not re.fullmatch(r"[0-9a-f]{24}\.txt", path.name):
            continue
        sha = archive_file(repo, path, "legacy-audited-text-cache", "text/plain")
        if sha not in archived:
            unbound += 1
        archived.add(sha)
        metadata.append({"originalPath": str(path.relative_to(root)), "sha256": sha})
    return {"manifests": len(manifests), "selectedRowsWithText": count, "distinctDocuments": len(linked),
            "rawTextArtifacts": len(archived), "unboundTextArtifacts": unbound,
            "sourceManifestSha256": repo.json_artifact(metadata, "legacy-fulltext-cache-manifest")}
