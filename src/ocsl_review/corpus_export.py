# SPDX-License-Identifier: Apache-2.0
"""Re-derive report facts from accepted database observations and export evidence."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import importlib.metadata
import shutil
from pathlib import Path

from .corpus_store import canonical, identity
from .corpus_library import now, digest_file
from .util import atomic_write


def import_snapshot(workspace, destination, source_path):
    """Bring a verified external seed into this chat before branching it.

    Snapshot files are immutable, so they are hard-linked when the seed is on the
    same filesystem and copied otherwise; the copy is verified again.
    """
    from .corpus_library import Workspace, Library, link_or_copy
    from .errors import ValidationError
    source = Path(source_path).absolute()
    if any(p.is_symlink() for p in (source, *source.parents)):
        raise ValidationError("external snapshot must not use symlinks")
    original = Library(Workspace(source.parent), source.name)
    verified = original.verify(require_snapshot=True)
    selected = Workspace(workspace)
    with selected.create(destination) as target:
        if verified.get("format_version") == 2:
            (target / "packs").mkdir(mode=0o700)
            for item in original.manifest()["files"]:
                link_or_copy(original.root / item["path"], target / item["path"])
        else:
            shutil.copyfile(original.db, target / "corpus.sqlite3")
            with original.connect() as connection:
                for artifact in connection.execute("SELECT * FROM source_artifacts"):
                    shutil.copyfile(original.source(artifact), target / artifact["relative_path"])
                    (target / artifact["relative_path"]).chmod(0o444)
            (target / "corpus.sqlite3").chmod(0o444)
        shutil.copyfile(source / "manifest.json", target / "manifest.json")
        (target / "manifest.json").chmod(0o444)
        captured = Library(selected, destination)
        result = captured.verify(require_snapshot=True)
        if result["manifest_sha256"] != verified["manifest_sha256"]:
            raise ValidationError("snapshot manifest changed during import")
    return {"library": destination, "manifestSha256": verified["manifest_sha256"], "counts": result["counts"],
            "merkleRoot": result.get("merkle_root")}


def implementation_manifest():
    home = Path(__file__).resolve().parents[2]
    files = sorted((home / "src/ocsl_review").glob("*.py"))
    files += sorted((home / "schemas").glob("*.json")) + sorted((home / "schemas").glob("*.sql"))
    files += sorted((home / "prompts").glob("*.txt"))
    files += [home / "pyproject.toml", home / "skills/ocsl-review-reference-analysis/assets/literature-schema.sql"]
    files += [home / "LICENSE"] + sorted((home / "LICENSES").glob("*.txt"))
    records = [{"path": str(path.relative_to(home)), "sha256": digest_file(path)} for path in files]
    return {"format": "ocsl-corpus-implementation-v1", "files": records, "sha256": identity(records)}


# Instruments the corpus executor judges with; a study bundle pins their registry digests.
EXECUTOR_INSTRUMENTS = ("citation-support@2",)
STUDY_BUNDLE_PREFIX = "study-bundle-sha256:"
SOURCE_TREE_PREFIX = "source-tree-sha256:"


def package_tags(manifest=None):
    """Package version and source-tree digest: provenance of a pin, never compared at dispatch."""
    try:
        version = importlib.metadata.version("ocsl-review")
    except importlib.metadata.PackageNotFoundError:
        import tomllib
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    return {"ocsl-review": version, "source-tree-sha256": (manifest or implementation_manifest())["sha256"]}


def study_bundle(protocol, *, study_prompt=b"", dataset_versions=None, manifest=None):
    """The study-bundle pin of a new analysis: protocol, study prompt, instrument digests, datasets, packages.

    Execution code is outside the pin, so executor fixes never force a new analysis;
    the code that served each call is recorded per receipt instead.
    """
    from .instruments import load, study_bundle_digest
    return study_bundle_digest(protocol=protocol, study_prompt=study_prompt,
                               instruments={instrument: load(instrument).digest for instrument in EXECUTOR_INSTRUMENTS},
                               dataset_versions=dataset_versions or {}, package_tags=package_tags(manifest))


def record_study_bundle(repo, analysis_id, bundle):
    digest = repo.json_artifact(bundle, "study-bundle")
    with repo.connect(write=True) as connection:
        connection.execute("INSERT OR IGNORE INTO processing_state VALUES (?,?,?,'completed',0,?,?)",
                           (analysis_id, "study-bundle", "pin", canonical({**bundle, "artifactSha256": digest}), now()))
    return digest


def pinned_study_bundle(repo, analysis):
    """The recorded bundle behind a study-bundle pin (calibration and validation variants use their parent's)."""
    from .errors import ValidationError
    from .instruments import canonical as bundle_canonical, sha256_bytes
    expected = analysis["code_revision"][len(STUDY_BUNDLE_PREFIX):]
    current, seen, row = analysis, set(), None
    while current is not None and current["analysis_id"] not in seen:
        seen.add(current["analysis_id"])
        with repo.connect() as connection:
            row = connection.execute("SELECT checkpoint_json FROM processing_state WHERE analysis_id=? AND "
                                     "task_key='study-bundle' AND phase='pin'", (current["analysis_id"],)).fetchone()
        if row is not None:
            break
        parent = current["protocol"].get("parentAnalysisId")
        current = repo.analysis(parent) if isinstance(parent, str) and parent else None
    if row is None:
        raise ValidationError("the analysis's study bundle is not recorded; branch a new analysis")
    bundle = json.loads(row[0])
    bundle.pop("artifactSha256", None)
    record = {key: value for key, value in bundle.items() if key != "sha256"}
    if bundle.get("sha256") != expected or sha256_bytes(bundle_canonical(record).encode("utf-8")) != expected:
        raise ValidationError("the recorded study bundle differs from the analysis pin")
    return bundle


def check_study_bundle(repo, analysis):
    """Refuse dispatch when an instrument the analysis pinned has changed (prompt, schema or validator)."""
    from .errors import ValidationError
    from .instruments import load
    bundle = pinned_study_bundle(repo, analysis)
    for instrument in EXECUTOR_INSTRUMENTS:
        try:
            current = load(instrument).digest
        except ValidationError as error:
            raise ValidationError(f"instrument {instrument} no longer verifies; branch a new analysis ({error})") from None
        if bundle["instruments"].get(instrument) != current:
            raise ValidationError(f"instrument {instrument} changed since the analysis was pinned; branch a new analysis")
    return bundle


def record_implementation(repo, manifest=None):
    manifest = manifest or implementation_manifest()
    home = Path(__file__).resolve().parents[2]
    for record in manifest["files"]:
        digest = repo.artifact((home / record["path"]).read_bytes(), "implementation-source", "text/plain")
        if digest != record["sha256"]:
            from .errors import ValidationError
            raise ValidationError("implementation changed during recording")
    import sys
    packages = {}
    for name in ("jsonschema", "referencing", "attrs", "rpds-py", "jinja2", "PyYAML"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    repo.json_artifact({"python": sys.version, "packages": packages}, "implementation-environment")
    return repo.json_artifact(manifest, "implementation-manifest")


FACT_QUERIES = {
    "reviewPopulation": "SELECT inclusion_state AS state,count(*) AS count FROM reviews GROUP BY inclusion_state ORDER BY state",
    "bibliographyIdentity": "SELECT identity_state AS state,count(*) AS count FROM bibliography_entries GROUP BY identity_state ORDER BY state",
    "referenceEntries": "SELECT count(*) AS count FROM bibliography_entries",
    "citationLoci": "SELECT count(*) AS count FROM claim_loci",
    "uniqueResolvedWorks": "SELECT count(DISTINCT resolved_work_id) AS count FROM bibliography_entries",
    "evidenceCoverage": "SELECT evidence_tier,complete,count(DISTINCT work_id) AS works FROM corpus_documents GROUP BY evidence_tier,complete ORDER BY evidence_tier,complete",
    "verdicts": "WITH ranked AS (SELECT *,row_number() OVER(PARTITION BY locus_id,evidence_level ORDER BY created_at DESC,assessment_id DESC) AS rank FROM assessments WHERE analysis_id=? AND validation_state='accepted') SELECT evidence_level,json_extract(verdict_json,'$.supportVerdict') AS verdict,json_extract(verdict_json,'$.evidenceCoverage') AS coverage,count(*) AS loci FROM ranked WHERE rank=1 GROUP BY evidence_level,verdict,coverage ORDER BY evidence_level,verdict,coverage",
    "taskStates": "SELECT tier,state,reason,count(*) AS count FROM corpus_tasks WHERE analysis_id=? GROUP BY tier,state,reason ORDER BY tier,state,reason",
    "modelUsage": "SELECT provider,model,count(*) AS calls,sum(coalesce(json_extract(usage_json,'$.inputTokens'),json_extract(usage_json,'$.input'),0)) AS input_tokens,sum(coalesce(json_extract(usage_json,'$.outputTokens'),json_extract(usage_json,'$.output'),0)) AS output_tokens FROM model_receipts WHERE analysis_id=? GROUP BY provider,model ORDER BY provider,model",
}


# Rows per keyset page of the input digest; memory stays at one page whatever the table size.
DIGEST_PAGE_ROWS = 5000


def keyset_pages(connection, source, column, condition="", params=(), *, page=None):
    """Yield the values of a unique column in ascending order, as lists of at most ``page`` values.

    Each page is one query that continues after the previous page's last value
    (``column > ?``) along the column's index, so there is no OFFSET and no sort; in a branch,
    SQLite merges the index ranges of the branch table and the parent table behind the union
    view. NULLs come first, where ORDER BY puts them. A ``condition`` on an indexed column must
    disable that index (``+column=?``) so that the scan keeps walking ``column``'s index.
    """
    page = page or DIGEST_PAGE_ROWS
    extra = f" AND ({condition})" if condition else ""
    cursor = connection.cursor()
    cursor.row_factory = None
    nulls = cursor.execute(f"SELECT count(*) FROM {source} WHERE {column} IS NULL{extra}", params).fetchone()[0]
    for start in range(0, nulls, page):
        yield [None] * min(page, nulls - start)
    values = [row[0] for row in cursor.execute(
        f"SELECT {column} FROM {source} WHERE {column} IS NOT NULL{extra} ORDER BY {column} LIMIT ?", (*params, page))]
    while values:
        yield values
        if len(values) < page:
            return
        values = [row[0] for row in cursor.execute(
            f"SELECT {column} FROM {source} WHERE {column} > ?{extra} ORDER BY {column} LIMIT ?", (values[-1], *params, page))]


class Scan:
    """A JSON array whose items arrive as consecutive lists (pages), so a digest never holds the whole array."""

    __slots__ = ("pages",)

    def __init__(self, pages):
        self.pages = pages


def streamed_identity(*values):
    """``identity(*values)`` where any array may be a ``Scan``: the same canonical JSON, hashed as it is produced."""
    digest = hashlib.sha256()

    def emit(value):
        if isinstance(value, Scan):
            digest.update(b"[")
            separator = b""
            for page in value.pages:
                if page:
                    # canonical(page) is "[item,...,item]": hash its items and keep the array open.
                    digest.update(separator + canonical(page)[1:-1].encode())
                    separator = b","
            digest.update(b"]")
        elif isinstance(value, dict) and all(isinstance(key, str) for key in value):
            digest.update(b"{")
            for number, key in enumerate(sorted(value)):
                digest.update(("," if number else "").encode() + canonical(key).encode() + b":")
                emit(value[key])
            digest.update(b"}")
        elif isinstance(value, (list, tuple)):
            digest.update(b"[")
            for number, item in enumerate(value):
                if number:
                    digest.update(b",")
                emit(item)
            digest.update(b"]")
        else:
            digest.update(canonical(value).encode())

    emit(values)
    return digest.hexdigest()


def read_facts(connection, analysis):
    """The facts of ``analysis`` and the digest of their inputs, read in one transaction.

    The facts and every page of the digest see the same database state. The digest is
    ``identity`` of the analysis, its protocol digest, every registered artifact digest
    (fact derivations excepted), the analysis's assessment ids and the facts' digest, with
    both lists streamed in key order.
    """
    connection.execute("BEGIN")
    try:
        result = {key: [dict(r) for r in connection.execute(sql, (analysis["analysis_id"],) if "?" in sql else ())]
                  for key, sql in FACT_QUERIES.items()}
        input_digest = streamed_identity({
            "analysis": analysis["analysis_id"], "protocolSha256": analysis["protocol_sha256"],
            "artifacts": Scan(keyset_pages(connection, "source_artifacts", "sha256", "artifact_kind!='fact-derivation'")),
            "assessments": Scan(keyset_pages(connection, "assessments", "assessment_id", "+analysis_id=?",
                                             (analysis["analysis_id"],))),
            "factsSha256": identity(result)})
    finally:
        connection.rollback()
    return result, input_digest


def derive_facts(repo, analysis_id=None):
    analysis = repo.analysis(analysis_id)
    with repo.connect() as connection:
        result, input_digest = read_facts(connection, analysis)
    derivation = repo.json_artifact({"version": 2, "queries": FACT_QUERIES}, "fact-derivation")
    with repo.connect(write=True) as connection:
        for key, value in result.items():
            connection.execute("INSERT OR IGNORE INTO facts VALUES (?,?,?,?,?,?,?)", (
                "fact-" + identity(analysis["analysis_id"], key, input_digest), analysis["analysis_id"], key,
                canonical(value), derivation, input_digest, now()))
    return {"analysisId": analysis["analysis_id"], "protocolSha256": analysis["protocol_sha256"],
            "codeRevision": analysis["code_revision"],
            "inputManifestSha256": input_digest, "derivationSha256": derivation, "facts": result}


def export_corpus(repo, relative="results/corpus"):
    output = repo.workspace.path(relative)
    if output == repo.root or repo.root in output.parents:
        from .errors import ValidationError
        raise ValidationError("exports must be separate from the mutable library")
    output.mkdir(parents=True, exist_ok=True)
    facts = derive_facts(repo)
    atomic_write(output / "facts.json", json.dumps(facts, ensure_ascii=False, indent=2) + "\n")
    queries = {
        "reviews": "SELECT r.*,w.title,w.publication_year,c.phase,c.state,c.reason FROM reviews r JOIN works w USING(work_id) JOIN corpus_reviews c USING(review_id) ORDER BY r.review_id",
        "references": "SELECT * FROM bibliography_entries ORDER BY entry_id",
        "citation_loci": "SELECT * FROM claim_loci ORDER BY locus_id",
        "assessments": "SELECT * FROM assessments WHERE analysis_id=? ORDER BY assessment_id",
        "receipts": "SELECT * FROM model_receipts WHERE analysis_id=? ORDER BY receipt_id",
        "legacy_observations": "SELECT * FROM work_observations ORDER BY observation_id",
    }
    manifest = []
    with repo.connect() as connection:
        for name, sql in queries.items():
            cursor = connection.execute(sql, (facts["analysisId"],) if "?" in sql else ())
            target = output / (name + ".csv")
            temporary = output / (name + ".csv.tmp")
            count = 0
            with temporary.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow([d[0] for d in cursor.description])
                for row in cursor:
                    writer.writerow(tuple(row)); count += 1
            temporary.replace(target)
            manifest.append({"path": target.name, "rows": count, "bytes": target.stat().st_size,
                             "sha256": digest_file(target)})
    manifest.append({"path": "facts.json", "sha256": hashlib.sha256((output / "facts.json").read_bytes()).hexdigest()})
    atomic_write(output / "manifest.json", json.dumps({"version": 2, "analysisId": facts["analysisId"], "files": manifest}, indent=2) + "\n")
    return {"output": relative, "files": manifest, "facts": facts["facts"]}
