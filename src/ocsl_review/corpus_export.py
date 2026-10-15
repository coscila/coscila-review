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
    """Copy a verified external seed into this chat before branching it."""
    from .corpus_library import Workspace, Library
    from .errors import ValidationError
    source = Path(source_path).absolute()
    if any(p.is_symlink() for p in (source, *source.parents)):
        raise ValidationError("external snapshot must not use symlinks")
    original = Library(Workspace(source.parent), source.name)
    verified = original.verify(require_snapshot=True)
    selected = Workspace(workspace)
    with selected.create(destination) as target:
        shutil.copyfile(original.db, target / "corpus.sqlite3")
        with original.connect() as connection:
            for artifact in connection.execute("SELECT * FROM source_artifacts"):
                shutil.copyfile(original.source(artifact), target / artifact["relative_path"])
                (target / artifact["relative_path"]).chmod(0o444)
        shutil.copyfile(source / "manifest.json", target / "manifest.json")
        captured = Library(selected, destination)
        result = captured.verify(require_snapshot=True)
        if result["manifest_sha256"] != verified["manifest_sha256"]:
            raise ValidationError("snapshot manifest changed during import")
        (target / "corpus.sqlite3").chmod(0o444)
        (target / "manifest.json").chmod(0o444)
    return {"library": destination, "manifestSha256": verified["manifest_sha256"], "counts": result["counts"]}


def implementation_manifest():
    home = Path(__file__).resolve().parents[2]
    files = sorted((home / "src/ocsl_review").glob("*.py"))
    files += sorted((home / "schemas").glob("*.json")) + sorted((home / "schemas").glob("*.sql"))
    files += sorted((home / "prompts").glob("*.txt"))
    files += [home / "pyproject.toml", home / "skills/ocsl-review-reference-analysis/assets/literature-schema.sql"]
    files += [home / "LICENSE"] + sorted((home / "LICENSES").glob("*.txt"))
    records = [{"path": str(path.relative_to(home)), "sha256": digest_file(path)} for path in files]
    return {"format": "ocsl-corpus-implementation-v1", "files": records, "sha256": identity(records)}


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


def derive_facts(repo, analysis_id=None):
    analysis = repo.analysis(analysis_id)
    with repo.connect() as connection:
        result = {key: [dict(r) for r in connection.execute(sql, (analysis["analysis_id"],) if "?" in sql else ())]
                  for key, sql in FACT_QUERIES.items()}
        inputs = {"analysis": analysis["analysis_id"], "protocolSha256": analysis["protocol_sha256"],
                  "artifacts": [r[0] for r in connection.execute("SELECT sha256 FROM source_artifacts WHERE artifact_kind!='fact-derivation' ORDER BY sha256")],
                  "assessments": [r[0] for r in connection.execute("SELECT assessment_id FROM assessments WHERE analysis_id=? ORDER BY assessment_id", (analysis["analysis_id"],))],
                  "factsSha256": identity(result)}
    derivation = repo.json_artifact({"version": 2, "queries": FACT_QUERIES}, "fact-derivation")
    input_digest = identity(inputs)
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
