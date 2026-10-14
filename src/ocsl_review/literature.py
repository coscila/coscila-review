"""Confirmed, resumable literature acquisition and evidence recording.

The workspace ledger owns acquisition progress. Content-addressed graph snapshots
make an interrupted projection recoverable without accepting stale judgments.
Downloading a document never creates a support verdict or a reading record.
"""

from __future__ import annotations

import copy
import fcntl
import json
import mimetypes
import os
import stat
from collections import Counter
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .audit import build_audit_summary, validate_audit_graph
from .claim_inventory import build_claim_inventory
from .confidentiality import _assert_no_symlink_components, load_scholarly_context
from .errors import ConfidentialityError, ReviewError, ValidationError
from .evidence import complete_abstract
from .scholarly import ScholarlyClient, utc_now
from .util import atomic_json, atomic_write, sha256_bytes, sha256_file

LEDGER_PATH = ".ocsl-review/literature-audit.json"
SCHEMA_VERSION = "1.0"
MAX_SOURCE_BYTES = 100 * 1024 * 1024
MAX_STATE_BYTES = 64 * 1024 * 1024
TERMINAL_ACQUISITION = {"available", "inaccessible", "blocked", "failed"}


def digest(value: Any) -> str:
    return sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode("utf-8"))


def _path(workspace: Path, value: str | Path, *, exists: bool = False) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = Path(os.path.abspath(candidate))
    _assert_no_symlink_components(candidate, label="literature artifact")
    if candidate == workspace or workspace not in candidate.parents:
        raise ValidationError("literature artifact escapes the selected workspace")
    if exists and not candidate.is_file():
        raise ValidationError(f"literature artifact is missing: {candidate.name}")
    if candidate.exists() and not candidate.is_file():
        raise ValidationError("literature artifact must be a regular file")
    return candidate


def _read_json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_STATE_BYTES:
        raise ValidationError("literature JSON exceeds its size bound")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValidationError(f"invalid literature JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValidationError("literature JSON must be an object")
    return value


def _workspace(args: Any) -> Path:
    from .workflow import _workspace as review_workspace

    lexical = Path(os.path.abspath(Path(args.workspace).expanduser()))
    _assert_no_symlink_components(lexical, label="literature workspace")
    return review_workspace(args)


@contextmanager
def _locked(workspace: Path) -> Iterator[None]:
    root = workspace / ".ocsl-review"
    _assert_no_symlink_components(root, label="literature directory")
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    lock = _path(workspace, ".ocsl-review/literature.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            raise ValidationError("literature lock must be an unshared regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _inputs(args: Any, workspace: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    from .workflow import _validate_schema

    manuscript = _read_json(_path(workspace, args.manuscript, exists=True))
    _validate_schema(args.paths, manuscript, "manuscript.schema.json")
    graph_path = _path(workspace, args.audit_graph)
    graph = _read_json(graph_path) if graph_path.exists() else build_claim_inventory(manuscript)
    if (graph.get("manuscript", {}).get("id") != manuscript["manuscriptId"]
            or graph.get("manuscript", {}).get("sourceSha256") != manuscript["source"]["sha256"]):
        raise ValidationError("audit graph does not belong to this manuscript version; prepare it again")
    validate_audit_graph(graph)
    return manuscript, graph


def _scope(manuscript: Mapping[str, Any], graph: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "manuscriptSha256": manuscript["source"]["sha256"],
        "bibliographySha256": digest([
            {"id": ref["id"], "raw": ref["raw"]} for ref in graph["references"]
        ]),
        "mode": "extensive",
        "correctness": True,
        "completeness": True,
        "searchScope": "bibliography-and-targeted-gaps",
        "unavailablePolicy": "continue-with-gaps",
    }


def _binding_digest(graph: Mapping[str, Any]) -> str:
    return digest({
        "claims": [{key: c.get(key) for key in (
            "id", "text", "locator", "citationClusterIds", "citationBindingStatus",
            "citationRequired", "requiredEvidenceLevel",
        )} for c in graph["claims"]],
        "clusters": [{k: v for k, v in c.items() if k != "processed"}
                     for c in graph["citationClusters"]],
    })


def _validate_state(state: dict[str, Any]) -> None:
    import jsonschema

    schema = _read_json(Path(__file__).resolve().parents[2]
                        / "schemas/literature-audit.schema.json")
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(state))
    if errors:
        raise ValidationError(f"invalid literature ledger: {errors[0].message}")
    ids = [entry["referenceId"] for entry in state["entries"]]
    if len(set(ids)) != len(ids):
        raise ValidationError("literature ledger contains duplicate reference IDs")
    if digest(state["scope"]) != state["scopeSha256"]:
        raise ValidationError("literature scope digest differs")
    for entry in state["entries"]:
        source_id = entry["sourceId"]
        if source_id is not None and source_id not in state["sources"]:
            raise ValidationError("literature entry names an unknown source artifact")


def _projection_digest(workspace: Path, graph_path: str) -> str | None:
    projection = _path(workspace, graph_path)
    return digest(_read_json(projection)) if projection.exists() else None


def _recoverable_graph_hashes(state: Mapping[str, Any]) -> set[str]:
    hashes = {state.get("graphSha256"), state.get("projectionRecoverySha256")}
    if "projectionRecoverySha256" not in state:
        # Earlier ledgers did not name the interrupted projection explicitly.
        # Only an already recorded graph version is eligible for migration.
        for event in state.get("history", []):
            hashes.update((event.get("graphSha256"), event.get("previousGraphSha256")))
    return {value for value in hashes if value is not None}


def _check_projection(workspace: Path, state: Mapping[str, Any]) -> str | None:
    actual = _projection_digest(workspace, state["graphPath"])
    if actual is not None and actual not in _recoverable_graph_hashes(state):
        raise ValidationError(
            "audit graph projection contains external edits; prepare those edits explicitly "
            "before resuming the managed literature review"
        )
    return actual


def _save(workspace: Path, state: dict[str, Any], graph: dict[str, Any], *, event: str) -> None:
    validate_audit_graph(graph)
    projection_hash = _check_projection(workspace, state)
    graph_hash = digest(graph)
    snapshot = _path(workspace, f".ocsl-review/literature/graphs/{graph_hash}.json")
    if snapshot.exists():
        if digest(_read_json(snapshot)) != graph_hash:
            raise ValidationError("stored graph snapshot has changed")
    else:
        atomic_json(snapshot, graph, mode=0o600)
    previous = state.get("graphSha256")
    state["graphSha256"] = graph_hash
    state["projectionRecoverySha256"] = projection_hash
    state["updatedAt"] = utc_now()
    state["history"].append({"at": state["updatedAt"], "event": event,
                             "previousGraphSha256": previous, "graphSha256": graph_hash})
    _validate_state(state)
    # The ledger is the commit point. Projection failures leave a valid canonical
    # snapshot which fetch/record can project again; stale projections cannot pass
    # the guard used by ordinary audit/package/render commands.
    atomic_json(_path(workspace, LEDGER_PATH), state, mode=0o600)
    atomic_json(_path(workspace, state["graphPath"]), graph, mode=0o600)


def _load(args: Any, workspace: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    state = _read_json(_path(workspace, LEDGER_PATH, exists=True))
    _validate_state(state)
    manuscript = _read_json(_path(workspace, state["manuscriptPath"], exists=True))
    graph = _read_json(_path(workspace,
        f".ocsl-review/literature/graphs/{state['graphSha256']}.json", exists=True))
    if digest(graph) != state["graphSha256"]:
        raise ValidationError("literature graph snapshot hash differs")
    _check_projection(workspace, state)
    if digest(_scope(manuscript, graph)) != state["scopeSha256"]:
        raise ValidationError("manuscript or bibliography changed; prepare and confirm the new scope")
    if state["bindingSha256"] != _binding_digest(graph):
        raise ValidationError("citation binding changed; prepare and reassess affected citations")
    if {e["referenceId"] for e in state["entries"]} != {r["id"] for r in _all_references(graph)}:
        raise ValidationError("literature reference universe differs from the audit graph")
    validate_audit_graph(graph)
    return state, graph


def _require_confirmation(state: Mapping[str, Any]) -> None:
    confirmation = state.get("confirmation")
    if (not confirmation or confirmation.get("decision") != "accepted"
            or confirmation.get("scopeSha256") != state["scopeSha256"]):
        raise ValidationError("extensive literature review requires the user's confirmation")


def _entry(state: dict[str, Any], reference_id: str) -> dict[str, Any]:
    for entry in state["entries"]:
        if entry["referenceId"] == reference_id:
            return entry
    raise ValidationError(f"unknown bibliography reference: {reference_id}")


def _ref(graph: dict[str, Any], reference_id: str) -> dict[str, Any]:
    return next(r for r in _all_references(graph) if r["id"] == reference_id)


def _all_references(graph: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [*graph["references"], *graph.get("additionalReferences", [])]


def _invalidate_reference(graph: dict[str, Any], reference_id: str) -> None:
    invalid_links = {link["id"] for link in graph["verificationLinks"]
                     if link["referenceId"] == reference_id}
    invalid_exams = {exam["id"] for exam in graph.get("sourceExaminations", [])
                     if reference_id in exam["referenceIds"]}
    affected = {link["claimId"] for link in graph["verificationLinks"]
                if link["referenceId"] == reference_id}
    affected.update(item["claimId"] for item in graph["completenessAssessments"]
                    if invalid_exams.intersection(item.get("sourceExaminationIds", [])))
    graph["verificationLinks"] = [v for v in graph["verificationLinks"]
                                  if v["referenceId"] != reference_id]
    for exam in graph.get("sourceExaminations", []):
        if reference_id in exam["referenceIds"]:
            exam["referenceIds"].remove(reference_id)
            exam["referenceDigests"].pop(reference_id, None)
    graph["sourceExaminations"] = [s for s in graph.get("sourceExaminations", [])
                                    if s["referenceIds"]]
    graph["completenessAssessments"] = [c for c in graph["completenessAssessments"]
                                        if c["claimId"] not in affected]
    invalid_citations = invalid_links | invalid_exams | {reference_id}
    graph["reviewAssertions"] = [a for a in graph["reviewAssertions"]
                                 if not invalid_citations.intersection(a.get("citationIds", []))]
    for claim in graph["claims"]:
        if claim["id"] in affected:
            claim["processed"] = False
    for cluster in graph["citationClusters"]:
        if reference_id in cluster["referenceIds"]:
            cluster["processed"] = False


def prepare_literature(
    workspace: Path, manuscript: dict[str, Any], graph: dict[str, Any], *,
    manuscript_path: str = ".ocsl-review/manuscript.json",
    graph_path: str = ".ocsl-review/audit-graph.json",
) -> tuple[dict[str, Any], dict[str, Any]]:
    scope = _scope(manuscript, graph)
    graph = copy.deepcopy(graph)
    graph["schemaVersion"] = "1.1"
    graph.setdefault("sourceExaminations", [])
    old_path = _path(workspace, LEDGER_PATH)
    previous = _read_json(old_path) if old_path.exists() else None
    old_graph = None
    if previous:
        _validate_state(previous)
        old_graph = _read_json(_path(workspace,
            f".ocsl-review/literature/graphs/{previous['graphSha256']}.json", exists=True))
        if digest(old_graph) != previous["graphSha256"]:
            raise ValidationError("literature graph snapshot hash differs")
        validate_audit_graph(old_graph)
        if (
            graph_path == previous["graphPath"]
            and digest(graph) in _recoverable_graph_hashes(previous)
            and digest(_scope(manuscript, old_graph)) == previous["scopeSha256"]
        ):
            # An interrupted projection may still contain an older graph.
            # Preparation must recover committed acquisitions and judgments,
            # rather than rebuilding from that stale view.
            graph = copy.deepcopy(old_graph)
            scope = _scope(manuscript, graph)
        if (previous["scopeSha256"] == digest(scope)
                and previous["bindingSha256"] == _binding_digest(graph)
                and previous["graphSha256"] == digest(graph)):
            observed_projection = _check_projection(
                workspace, {**previous, "graphPath": graph_path}
            )
            previous["manuscriptPath"] = manuscript_path
            previous["graphPath"] = graph_path
            previous["projectionRecoverySha256"] = observed_projection
            return previous, graph
    # Old reports remain readable, but legacy judgments have no demonstrated
    # reading coverage. Replanning never fabricates the new evidence fields.
    for ref in _all_references(graph):
        _invalidate_reference(graph, ref["id"])
        ref["acquisition"] = {"state": "pending", "attempted": False, "limitations": []}
    state = {
        "schemaVersion": SCHEMA_VERSION,
        "auditId": graph["auditId"],
        "manuscriptId": manuscript["manuscriptId"],
        "manuscriptPath": manuscript_path,
        "graphPath": graph_path,
        "scope": scope,
        "scopeSha256": digest(scope),
        "bindingSha256": _binding_digest(graph),
        "confirmation": None,
        "entries": [{"referenceId": ref["id"], "sourceId": None,
                     "state": "pending", "attempts": [], "records": [],
                     "limitations": []} for ref in _all_references(graph)],
        "sources": copy.deepcopy(previous["sources"]) if previous else {},
        "history": copy.deepcopy(previous["history"]) if previous else [],
        "graphSha256": None,
        "projectionRecoverySha256": _projection_digest(workspace, graph_path),
        "updatedAt": utc_now(),
    }
    if previous:
        previous_entries = {entry["referenceId"]: entry for entry in previous["entries"]}
        current_claims = {claim["id"]: claim for claim in graph["claims"]}
        old_claims = {claim["id"]: claim for claim in old_graph["claims"]}
        for entry in state["entries"]:
            prior = previous_entries.get(entry["referenceId"], {})
            if "purposeClaimIds" not in prior:
                continue
            targets = prior["purposeClaimIds"]
            entry["purposeClaimIds"] = [identifier for identifier in targets if identifier in current_claims]
            changed = [identifier for identifier in targets if identifier not in current_claims
                       or old_claims.get(identifier, {}).get("text") != current_claims[identifier]["text"]]
            if changed:
                entry["limitations"].append(
                    "additional-source-targets-require-reassessment:" + ",".join(changed)
                )
    if previous and previous["scopeSha256"] == state["scopeSha256"]:
        state["confirmation"] = previous["confirmation"]
    return state, graph


def _store_source(workspace: Path, state: dict[str, Any], document: Mapping[str, Any], *,
                  access_level: str, provider: str) -> str:
    body = document["body"]
    if not isinstance(body, bytes) or not body or len(body) > MAX_SOURCE_BYTES:
        raise ValidationError("source must contain bounded, nonempty bytes")
    source_hash = sha256_bytes(body)
    if document.get("sha256", source_hash) != source_hash:
        raise ValidationError("source digest does not match its bytes")
    blocks = document.get("blocks", [])
    if (not isinstance(blocks, list) or any(not isinstance(b, dict) or
            not all(isinstance(b.get(key), str) and b[key].strip()
                    for key in ("id", "kind", "locator", "text")) for b in blocks)):
        raise ValidationError("source blocks need nonempty identifiers, locators, kinds, and text")
    if not blocks or not any(b.get("text", "").strip() for b in blocks):
        raise ValidationError("source contains no extractable evidence blocks")
    ids = [b["id"] for b in blocks]
    if len(set(ids)) != len(ids):
        raise ValidationError("source block identifiers must be unique")
    text = document["text"]
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("source text is empty")
    if not isinstance(document.get("complete", False), bool):
        raise ValidationError("source completeness must be a boolean")
    extracted = {"text": text, "blocks": blocks}
    if len(json.dumps(extracted, ensure_ascii=False, indent=2).encode("utf-8")) + 1 > MAX_STATE_BYTES:
        raise ValidationError("extracted source document exceeds its size bound")
    source_id = "source-" + digest({"body": source_hash, "text": text,
                                    "blocks": blocks, "accessLevel": access_level})
    relative = f".ocsl-review/literature/sources/{source_id}"
    source = {key: value for key, value in document.items()
              if key not in {"body", "text", "blocks"}}
    source.update({
        "id": source_id, "artifactPath": relative + "/source.bin",
        "documentPath": relative + "/document.json", "textPath": relative + "/text.txt",
        "artifactSha256": source_hash, "textSha256": sha256_bytes(text.encode("utf-8")),
        "documentSha256": digest({"text": text, "blocks": blocks}),
        "accessLevel": access_level, "provider": provider, "expectedUnitIds": ids,
        "readUnitIds": [], "retrievedAt": document.get("retrievedAt", utc_now()),
        "complete": bool(document.get("complete", False)),
    })
    if source_id in state["sources"]:
        _verified_source(workspace, state["sources"][source_id])
        return source_id
    atomic_write(_path(workspace, source["artifactPath"]), body, mode=0o600)
    atomic_write(_path(workspace, source["textPath"]), text, mode=0o600)
    atomic_json(_path(workspace, source["documentPath"]), {"text": text, "blocks": blocks}, mode=0o600)
    state["sources"][source_id] = source
    return source_id


def _verified_source(workspace: Path, source: Mapping[str, Any]) -> dict[str, Any]:
    for path_key, hash_key in (("artifactPath", "artifactSha256"), ("textPath", "textSha256")):
        path = _path(workspace, source[path_key], exists=True)
        if path.stat().st_nlink != 1 or sha256_file(path) != source[hash_key]:
            raise ValidationError("source artifact changed or shares mutable inodes; attach it again")
    document = _read_json(_path(workspace, source["documentPath"], exists=True))
    if digest(document) != source["documentSha256"]:
        raise ValidationError("extracted source document changed; attach it again")
    if [b["id"] for b in document["blocks"]] != source["expectedUnitIds"]:
        raise ValidationError("source reading universe differs from its extraction")
    return document


def _acquisition(entry: dict[str, Any], reference: dict[str, Any], state: str,
                 limitations: list[str]) -> None:
    entry["state"] = state
    entry["limitations"] = list(dict.fromkeys(limitations))
    reference["acquisition"] = {"state": state, "attempted": True,
                                "limitations": entry["limitations"]}


def _client(args: Any, workspace: Path) -> ScholarlyClient:
    plan, selected = load_scholarly_context(args.paths, os.environ, cwd=workspace)
    if selected != workspace:
        raise ValidationError(
            "literature fetch requires LAB_WORKSPACE_ROOT and a scholarly context bound "
            "to the selected workspace"
        )
    return ScholarlyClient(plan=plan, workspace=selected)


def _identifiers(reference: Mapping[str, Any]) -> dict[str, str]:
    return {key: str(reference["identifiers"][key]) for key in ("doi", "pmid", "pmcid")
            if reference.get("identifiers", {}).get(key)}


def _resolve(client: ScholarlyClient, entry: dict[str, Any], reference: dict[str, Any]) -> None:
    identifiers = _identifiers(reference)
    entry["candidateIdentifiers"] = dict(identifiers)
    entry["verifiedIdentifiers"] = {}
    entry.pop("publicationStatusEvidence", None)
    reference["publicationStatus"] = {"state": "unknown", "checkedAt": None, "providers": [],
                                      "details": ["Publication status requires verified work identifiers."]}
    if not identifiers:
        raise ConfidentialityError(
            "no public identifier: supply an identifier or a local source; printed titles "
            "are not sent as disguised search queries"
        )
    key = next(iter(identifiers))
    resolved = client.resolve_identifier(**{key: identifiers[key]})
    records = resolved["records"]
    entry["records"] = records
    # Provider-normalized identity from a previous attempt is not independent
    # evidence of what the manuscript actually printed.
    printed = {"raw": reference["raw"], **identifiers}
    if entry.get("purposeClaimIds"):
        agreement = client.reconcile_identifier_metadata(records, doi=identifiers["doi"])
    else:
        agreement = client.reconcile_reference_metadata(records, printed_reference=printed)
    strength = agreement.get("agreementStrength", "weak")
    reference["identityEvidence"] = {
        "agreementStrength": strength,
        "providersCompared": agreement.get("providersCompared", agreement.get("providers", [])),
        "missingFields": agreement.get("missingFields", []),
        "differences": agreement.get("differences", []),
    }
    reference["resolutionStatus"] = "resolved" if strength == "strong" else (
        "ambiguous" if strength == "conflict" else "unresolved")
    if records:
        selected = records[0]
        reference["identity"] = {
            "title": selected.get("title"), "authors": selected.get("authors", []),
            "year": selected.get("year"), "container": selected.get("container"),
            "version": selected.get("version"),
        }
        if strength == "strong":
            for record in records:
                for name in ("doi", "pmid", "pmcid"):
                    if record.get(name):
                        entry["verifiedIdentifiers"][name] = str(record[name])
    entry["limitations"].extend(resolved.get("limitations", []))
    # Publication notices must concern the resolved work, not an unverified
    # publisher link that happened to provide a PMID alongside its DOI.
    status_ids = entry["verifiedIdentifiers"]
    status_key = "doi" if status_ids.get("doi") else "pmid" if status_ids.get("pmid") else None
    if status_key:
        try:
            status_result = client.get_publication_status(**{status_key: status_ids[status_key]})
            entry["publicationStatusEvidence"] = status_result
            reference["publicationStatus"] = {
                "state": status_result["state"], "checkedAt": status_result["checkedAt"],
                "providers": status_result["providersChecked"],
                "details": [json.dumps(s, sort_keys=True) for s in status_result.get("signals", [])]
                           + status_result.get("limitations", []),
            }
        except ReviewError as exc:
            entry["limitations"].append(f"publication-status-unverified: {exc}")


def fetch_literature(workspace: Path, state: dict[str, Any], graph: dict[str, Any],
                     client: ScholarlyClient, *, reference_ids: list[str] | None = None,
                     retry: bool = False) -> None:
    _require_confirmation(state)
    selected = set(reference_ids or [e["referenceId"] for e in state["entries"]])
    if not selected <= {e["referenceId"] for e in state["entries"]}:
        raise ValidationError("fetch names an unknown bibliography reference")
    refreshed_references: set[str] = set()
    for entry in state["entries"]:
        ref_id = entry["referenceId"]
        if ref_id not in selected or (entry["state"] in TERMINAL_ACQUISITION and not retry):
            continue
        reference = _ref(graph, ref_id)
        _invalidate_reference(graph, ref_id)
        entry["sourceId"] = None
        _acquisition(entry, reference, "pending", [])
        attempt = {"at": utc_now(), "operation": "fetch", "state": "pending"}
        entry["attempts"].append(attempt)
        _save(workspace, state, graph, event="fetch-started:" + ref_id)
        try:
            _resolve(client, entry, reference)
            identifiers = _identifiers(reference)
            # Acquisition can be reused only after identity reconciliation and
            # only for exactly the same identifiers, not merely a matching title.
            reusable = next((e for e in state["entries"] if e is not entry
                             and e["sourceId"] and e.get("acquiredIdentifiers") == identifiers
                             and (not retry or e["referenceId"] in refreshed_references)), None)
            if reusable:
                _verified_source(workspace, state["sources"][reusable["sourceId"]])
                entry["sourceId"] = reusable["sourceId"]
                entry["abstractCandidates"] = copy.deepcopy(reusable.get("abstractCandidates", []))
            else:
                identifier_kind = next(iter(identifiers))
                result = client.acquire_full_text(**{identifier_kind: identifiers[identifier_kind]})
                entry["limitations"].extend(result.get("limitations", []))
                attempt["routes"] = result.get("attempts", [])
                entry["abstractCandidates"] = result.get("abstractCandidates", [])
                documents = result.get("documents", [])
                if documents:
                    entry["sourceId"] = _store_source(
                        workspace, state, documents[0], access_level="full-text",
                        provider=result.get("provider", "multiple"))
                elif result.get("abstract"):
                    abstract = result["abstract"]
                    candidate = next((candidate for candidate in entry["abstractCandidates"]
                                      if complete_abstract(candidate) and candidate["text"] == abstract), None)
                    complete = candidate is not None
                    provenance = candidate["provenance"] if candidate else {}
                    document = {
                        "body": abstract.encode("utf-8"), "mediaType": "text/plain",
                        "sourceUrl": provenance.get("sourceUrl"), "complete": complete, "text": abstract,
                        "identifiers": provenance.get("identifiers", {}), "abstractProvenance": provenance,
                        "blocks": [{"id": "abstract", "kind": "abstract", "locator": "Abstract",
                                    "text": abstract}],
                    }
                    entry["sourceId"] = _store_source(workspace, state, document,
                        access_level="abstract-only",
                        provider=result.get("abstractProvider") or result.get("provider", "multiple"))
            entry["acquiredIdentifiers"] = identifiers
            outcome = "available" if entry["sourceId"] else "inaccessible"
            _acquisition(entry, reference, outcome, entry["limitations"])
            if entry["sourceId"]:
                refreshed_references.add(ref_id)
        except ConfidentialityError as exc:
            _acquisition(entry, reference, "blocked", [*entry["limitations"], str(exc)])
        except (ReviewError, OSError, ValueError) as exc:
            _acquisition(entry, reference, "failed", [*entry["limitations"], str(exc)])
        if entry["sourceId"]:
            _upsert(graph.setdefault("sourceExaminations", []),
                    _examination(state, graph, entry["sourceId"]), "id")
        attempt["state"] = entry["state"]
        attempt["finishedAt"] = utc_now()
        _save(workspace, state, graph, event="fetch-finished:" + ref_id)


def literature_status(state: Mapping[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    entries = state["entries"]
    bibliography_ids = {r["id"] for r in graph["references"]}
    bibliography = [e for e in entries if e["referenceId"] in bibliography_ids]
    acquisition = Counter(e["state"] for e in bibliography)
    additional_acquisition = Counter(e["state"] for e in entries if e["referenceId"] not in bibliography_ids)
    sources = state["sources"]
    selected_sources = {e["sourceId"] for e in entries if e["sourceId"]}
    read = {s for s in selected_sources if sources[s]["complete"] and sources[s]["expectedUnitIds"]
            and set(sources[s]["readUnitIds"]) == set(sources[s]["expectedUnitIds"])}
    full_read = {s for s in read if sources[s]["complete"]
                 and sources[s]["accessLevel"] == "full-text"}
    return {
        "scopeSha256": state["scopeSha256"], "confirmation": state["confirmation"],
        "bibliographyEntries": len(bibliography),
        "additionalReferences": len(entries) - len(bibliography),
        "citationOccurrences": len(graph["citationClusters"]),
        "expandedReferenceMembers": sum(len(c["markers"]) for c in graph["citationClusters"]),
        "acquisition": {key: acquisition[key] for key in ("pending", *sorted(TERMINAL_ACQUISITION))},
        "additionalAcquisition": {key: additional_acquisition[key] for key in ("pending", *sorted(TERMINAL_ACQUISITION))},
        "uniqueDocumentsObtained": len(selected_sources), "uniqueDocumentsRead": len(read),
        "uniqueFullTextsRead": len(full_read),
        "acquisitionAccountedFor": bool(bibliography) and acquisition["pending"] == 0,
        "readingComplete": bool(entries) and all(e["sourceId"] in read for e in entries),
        "sources": [{"referenceId": e["referenceId"], "state": e["state"],
                     "source": sources.get(e["sourceId"]), "limitations": e["limitations"]}
                    for e in entries],
        "summary": build_audit_summary(graph),
    }


def cmd_prepare(args: Any) -> dict[str, Any]:
    workspace = _workspace(args)
    with _locked(workspace):
        manuscript, graph = _inputs(args, workspace)
        state, graph = prepare_literature(
            workspace, manuscript, graph,
            manuscript_path=str(_path(workspace, args.manuscript).relative_to(workspace)),
            graph_path=str(_path(workspace, args.audit_graph).relative_to(workspace)))
        _save(workspace, state, graph, event="prepared")
        return {**literature_status(state, graph), "proposal": (
            f"Review all {len(state['entries'])} bibliography entries and "
            f"{len(graph['citationClusters'])} citation occurrences extensively, obtain and read "
            "every accessible source, and investigate targeted missing or contrary evidence. "
            "This can take substantially longer than a metadata check. Continue with explicit "
            "abstract-based assessments or abstentions when full text is unavailable. "
            "Ask for confirmation unless the user has already explicitly requested this scope."
        )}


def cmd_confirm(args: Any) -> dict[str, Any]:
    workspace = _workspace(args)
    with _locked(workspace):
        state, graph = _load(args, workspace)
        if args.scope_sha256 != state["scopeSha256"]:
            raise ValidationError("confirmation names an obsolete or different review scope")
        if not args.basis.strip():
            raise ValidationError("confirmation requires the user's actual decision basis")
        if args.accept and not state["entries"]:
            raise ValidationError("cannot confirm an extensive audit with an empty bibliography")
        state["confirmation"] = {"decision": "accepted" if args.accept else "declined",
                                  "scopeSha256": state["scopeSha256"], "at": utc_now(),
                                  "basis": args.basis.strip()}
        _save(workspace, state, graph, event="confirmation-recorded")
        return literature_status(state, graph)


def cmd_fetch(args: Any) -> dict[str, Any]:
    workspace = _workspace(args)
    with _locked(workspace):
        state, graph = _load(args, workspace)
        _require_confirmation(state)
        client = _client(args, workspace)
        add_gap_sources(state, graph, args.additional_doi, args.claim_id)
        fetch_literature(workspace, state, graph, client,
                         reference_ids=args.reference_id, retry=args.retry)
        # Also repairs an interrupted projection when there was nothing to fetch.
        _save(workspace, state, graph, event="fetch-checkpoint")
        return literature_status(state, graph)


def add_gap_sources(state: dict[str, Any], graph: dict[str, Any], dois: list[str],
                    claim_ids: list[str]) -> None:
    from .scholarly import normalize_doi

    _require_confirmation(state)
    if not dois:
        if claim_ids:
            raise ValidationError("--claim-id requires --additional-doi")
        return
    known_claims = {c["id"] for c in graph["claims"]}
    if not claim_ids or not set(claim_ids) <= known_claims:
        raise ValidationError("additional sources require existing --claim-id targets")
    normalized = [normalize_doi(value) for value in dois]
    for doi in dict.fromkeys(normalized):
        reference_id = "additional-" + digest(doi)[:24]
        previous = next((e for e in state["entries"] if e["referenceId"] == reference_id), None)
        if previous:
            previous["purposeClaimIds"] = sorted(set(previous.get("purposeClaimIds", [])) | set(claim_ids))
            continue
        graph.setdefault("additionalReferences", []).append({
            "id": reference_id, "raw": "doi:" + doi, "identifiers": {"doi": doi},
            "identity": {"title": None, "authors": [], "year": None, "container": None, "version": None},
            "resolutionStatus": "unresolved", "publicationStatus": {
                "state": "unknown", "checkedAt": None, "providers": [], "details": []},
            "sourceRole": "other", "independenceGroup": None, "metadataProblems": [],
            "acquisition": {"state": "pending", "attempted": False, "limitations": []},
        })
        state["entries"].append({"referenceId": reference_id, "sourceId": None,
                                 "state": "pending", "attempts": [], "records": [],
                                 "limitations": [], "purposeClaimIds": sorted(set(claim_ids))})


def cmd_attach(args: Any) -> dict[str, Any]:
    from .fulltext import parse_source_document
    from .scholarly import normalize_doi

    workspace = _workspace(args)
    with _locked(workspace):
        state, graph = _load(args, workspace)
        _require_confirmation(state)
        if not args.reference_id:
            raise ValidationError("attach requires at least one --reference-id")
        for ref_id in args.reference_id:
            _entry(state, ref_id)
        path = _path(workspace, args.input, exists=True)
        with path.open("rb") as stream:
            body = stream.read(MAX_SOURCE_BYTES + 1)
        if len(body) > MAX_SOURCE_BYTES:
            raise ValidationError("attached source exceeds 100 MiB")
        media = args.media_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        document = parse_source_document(body, media, None)
        attachment_limitations: dict[str, list[str]] = {}
        for ref_id in args.reference_id:
            expected = _identifiers(_ref(graph, ref_id))
            observed = document.get("identifiers", {})
            shared = expected.keys() & observed.keys()
            for key in shared:
                if key == "doi":
                    try:
                        matches = normalize_doi(observed[key]) == normalize_doi(expected[key])
                    except ReviewError as exc:
                        raise ValidationError("attached source contains an invalid DOI") from exc
                elif key == "pmcid":
                    matches = observed[key].upper().removeprefix("PMC") == expected[key].upper().removeprefix("PMC")
                else:
                    matches = observed[key] == expected[key]
                if not matches:
                    raise ValidationError(f"attached source identifies a different source for {ref_id}")
            attachment_limitations[ref_id] = [] if shared else [
                "Local source identity is not independently established by shared identifiers; "
                "its association with this reference relies on the supplied attachment."
            ]
        source_id = _store_source(workspace, state, document,
                                  access_level=args.access_level, provider="local-file")
        for ref_id in args.reference_id:
            _invalidate_reference(graph, ref_id)
            entry = _entry(state, ref_id)
            entry["sourceId"] = source_id
            # Local association does not inherit a previous provider download's
            # identity proof and must not seed automatic network deduplication.
            entry.pop("acquiredIdentifiers", None)
            entry["attempts"].append({"operation": "attach", "at": utc_now(), "state": "available"})
            _acquisition(entry, _ref(graph, ref_id), "available", attachment_limitations[ref_id])
        _upsert(graph.setdefault("sourceExaminations", []),
                _examination(state, graph, source_id), "id")
        _save(workspace, state, graph, event="source-attached")
        return literature_status(state, graph)


def cmd_status(args: Any) -> dict[str, Any]:
    workspace = _workspace(args)
    state, graph = _load(args, workspace)
    for source_id in {e["sourceId"] for e in state["entries"] if e["sourceId"]}:
        _verified_source(workspace, state["sources"][source_id])
    return literature_status(state, graph)


def _examination(state: dict[str, Any], graph: dict[str, Any], source_id: str) -> dict[str, Any]:
    from .audit import reference_digest

    source = state["sources"][source_id]
    ref_ids = [e["referenceId"] for e in state["entries"] if e["sourceId"] == source_id]
    return {
        "id": "exam-" + source_id.removeprefix("source-"),
        "referenceIds": ref_ids,
        "referenceDigests": {r: reference_digest(_ref(graph, r)) for r in ref_ids},
        "artifactPath": source["artifactPath"],
        "artifactSha256": source["artifactSha256"],
        "textSha256": source["textSha256"],
        "accessLevel": source["accessLevel"],
        "contentComplete": source["complete"],
        "expectedUnitIds": source["expectedUnitIds"],
        "readUnitIds": source["readUnitIds"],
    }


def _upsert(records: list[dict[str, Any]], incoming: Mapping[str, Any], key: str) -> None:
    if not incoming.get(key):
        raise ValidationError(f"record requires {key}")
    for index, previous in enumerate(records):
        if previous[key] == incoming[key]:
            records[index] = dict(incoming)
            return
    records.append(dict(incoming))


def _refresh_processed(graph: dict[str, Any]) -> None:
    triples = {(v["claimId"], v["citationClusterId"], v["referenceId"])
               for v in graph["verificationLinks"] if v.get("citationClusterId")}
    clusters = {c["id"]: c for c in graph["citationClusters"]}
    for claim in graph["claims"]:
        expected = {(claim["id"], cid, rid) for cid in claim["citationClusterIds"]
                    for rid in clusters[cid]["referenceIds"]}
        if claim["citationRequired"]:
            claim["processed"] = (bool(expected) and expected <= triples
                                  and claim["citationBindingStatus"] != "ambiguous")
    for cluster in graph["citationClusters"]:
        expected = {(claim["id"], cluster["id"], rid) for claim in graph["claims"]
                    if cluster["id"] in claim["citationClusterIds"]
                    for rid in cluster["referenceIds"]}
        cluster["processed"] = bool(expected) and expected <= triples


def record_assessments(workspace: Path, state: dict[str, Any], graph: dict[str, Any],
                       record: Mapping[str, Any]) -> None:
    """Validate an agent's actual reading and judgments; never generate verdicts."""
    from .audit import citation_cluster_digest, claim_digest

    _require_confirmation(state)
    allowed = {"referenceId", "readUnitIds", "verificationLinks", "completenessAssessments",
               "searchRuns", "reviewAssertions", "referenceRecords", "identifiers", "claimRequirements"}
    if set(record) - allowed:
        raise ValidationError(f"unknown assessment fields: {sorted(set(record) - allowed)}")
    if not any(record.get(name) for name in allowed - {"referenceId"}):
        raise ValidationError("assessment record contains no reading, evidence, or decision")
    for name in allowed - {"referenceId", "identifiers"}:
        if name in record and not isinstance(record[name], list):
            raise ValidationError(f"{name} must be a list")
        if name in record and name != "readUnitIds" and not all(
            isinstance(item, dict) for item in record[name]
        ):
            raise ValidationError(f"{name} must contain objects")
    reference_id = record.get("referenceId")
    entry = _entry(state, reference_id) if reference_id else None
    source = None
    document = None
    examination = None
    for requirement in record.get("claimRequirements", []):
        if set(requirement) != {"claimId", "requiredEvidenceLevel", "reason"}:
            raise ValidationError("claimRequirements needs claimId, requiredEvidenceLevel, and reason")
        claim = next((c for c in graph["claims"] if c["id"] == requirement["claimId"]), None)
        if claim is None or not isinstance(requirement["reason"], str) or not requirement["reason"].strip():
            raise ValidationError("evidence requirement needs an existing claim and an explanation")
        level = requirement["requiredEvidenceLevel"]
        if level not in {"full-text", "abstract-sufficient"}:
            raise ValidationError("substantive claims need full-text or abstract-sufficient evidence")
        if level == "abstract-sufficient" and claim["claimType"] in {
            "numeric", "causal", "quotation", "method", "software", "dataset", "novelty"
        }:
            raise ValidationError("this claim type requires full-text examination")
        invalid_links = {v["id"] for v in graph["verificationLinks"] if v["claimId"] == claim["id"]}
        graph["verificationLinks"] = [v for v in graph["verificationLinks"] if v["claimId"] != claim["id"]]
        graph["reviewAssertions"] = [a for a in graph["reviewAssertions"]
                                     if not invalid_links.intersection(a.get("citationIds", []))]
        graph["completenessAssessments"] = [c for c in graph["completenessAssessments"] if c["claimId"] != claim["id"]]
        claim["requiredEvidenceLevel"] = level
        claim["evidenceRequirementReason"] = requirement["reason"].strip()
        claim["processed"] = False
    if record.get("identifiers") or record.get("referenceRecords"):
        if entry is None:
            raise ValidationError("reference metadata recording requires referenceId")
        reference = _ref(graph, reference_id)
        _invalidate_reference(graph, reference_id)
        if record.get("identifiers"):
            identifiers = record["identifiers"]
            if not isinstance(identifiers, dict) or not set(identifiers) <= {"doi", "pmid", "pmcid"}:
                raise ValidationError("identifiers must contain only DOI, PMID, or PMCID")
            from .confidentiality import IDENTIFIER_PATTERNS
            for key, value in identifiers.items():
                if not isinstance(value, str) or not IDENTIFIER_PATTERNS[key].fullmatch(value):
                    raise ValidationError(f"invalid {key} identifier")
            reference["identifiers"].update(identifiers)
            entry["candidateIdentifiers"] = dict(_identifiers(reference))
            entry["verifiedIdentifiers"] = {}
            entry.pop("publicationStatusEvidence", None)
            reference["publicationStatus"] = {"state": "unknown", "checkedAt": None, "providers": [],
                                              "details": ["Identifier changed; publication status requires fresh verification."]}
            reference["resolutionStatus"] = "unresolved"
            reference.pop("identityEvidence", None)
            entry["sourceId"] = None
            _acquisition(entry, reference, "pending", ["identifier-changed-requires-resolution"])
        if record.get("referenceRecords"):
            records = record["referenceRecords"]
            entry.pop("publicationStatusEvidence", None)
            reference["publicationStatus"] = {"state": "unknown", "checkedAt": None, "providers": [],
                                              "details": ["Metadata changed; publication status requires fresh verification."]}
            if entry.get("purposeClaimIds"):
                agreement = ScholarlyClient.reconcile_identifier_metadata(
                    records, doi=reference["identifiers"]["doi"])
            else:
                agreement = ScholarlyClient.reconcile_reference_metadata(
                    records, printed_reference={"raw": reference["raw"], **_identifiers(reference)})
            entry["records"] = copy.deepcopy(records)
            reference["identityEvidence"] = {key: agreement[key] for key in (
                "agreementStrength", "providersCompared", "missingFields", "differences")}
            strength = agreement["agreementStrength"]
            entry["candidateIdentifiers"] = dict(_identifiers(reference))
            entry["verifiedIdentifiers"] = {name: str(item[name]) for item in records for name in ("doi", "pmid", "pmcid")
                                            if strength == "strong" and item.get(name)}
            reference["resolutionStatus"] = ("resolved" if strength == "strong" else
                                               "ambiguous" if strength == "conflict" else "unresolved")
            if strength == "strong":
                selected = records[0]
                reference["identity"] = {
                    "title": selected.get("title"), "authors": selected.get("authors", []),
                    "year": selected.get("year"), "container": selected.get("container"),
                    "version": selected.get("version"),
                }
    if entry and entry["sourceId"]:
        source = state["sources"][entry["sourceId"]]
        document = _verified_source(workspace, source)
        reads = record.get("readUnitIds", [])
        if not all(isinstance(item, str) for item in reads):
            raise ValidationError("readUnitIds must contain strings")
        if not set(reads) <= set(source["expectedUnitIds"]):
            raise ValidationError("reading record names a unit absent from the complete source")
        source["readUnitIds"] = sorted(set(source["readUnitIds"]) | set(reads))
        examination = _examination(state, graph, entry["sourceId"])
        _upsert(graph.setdefault("sourceExaminations", []), examination, "id")
    elif record.get("readUnitIds"):
        raise ValidationError("cannot record reading without an acquired source")

    claims = {c["id"]: c for c in graph["claims"]}
    clusters = {c["id"]: c for c in graph["citationClusters"]}
    for supplied in record.get("verificationLinks", []):
        link = copy.deepcopy(supplied)
        if entry is None:
            raise ValidationError("verification recording requires referenceId")
        if link.get("referenceId", reference_id) != reference_id:
            raise ValidationError("verification link belongs to another reference")
        claim_id, cluster_id = link.get("claimId"), link.get("citationClusterId")
        if claim_id not in claims or cluster_id not in clusters:
            raise ValidationError("verification link names an unknown claim or citation occurrence")
        if (cluster_id not in claims[claim_id]["citationClusterIds"]
                or reference_id not in clusters[cluster_id]["referenceIds"]):
            raise ValidationError("verification does not match this claim–citation–reference binding")
        link["referenceId"] = reference_id
        link["id"] = "link-" + digest([claim_id, cluster_id, reference_id])[:24]
        link["claimSha256"] = claim_digest(claims[claim_id])
        link["citationClusterSha256"] = citation_cluster_digest(clusters[cluster_id])
        link["sourceExaminationId"] = examination["id"] if examination else None
        actual_level = source["accessLevel"] if source else "inaccessible"
        link["access"] = {
            "level": actual_level, "provider": source["provider"] if source else "none",
            "uri": source.get("sourceUrl") if source else None,
            "checkedAt": utc_now(),
        }
        link.setdefault("evidenceTier", actual_level)
        blocks = {b["id"]: b for b in document["blocks"]} if document else {}
        for location in link.get("evidenceLocations", []):
            unit_id = location.get("unitId")
            if unit_id not in blocks:
                raise ValidationError("evidence locator names a unit absent from the acquired source")
            block = blocks[unit_id]
            excerpt = location.get("excerpt")
            if excerpt is not None and (not excerpt or excerpt not in block["text"]):
                raise ValidationError("evidence excerpt is not present in the named source unit")
            actual_hash = sha256_bytes((excerpt if excerpt is not None else block["text"]).encode("utf-8"))
            if location.get("excerptSha256", actual_hash) != actual_hash:
                raise ValidationError("evidence excerpt hash differs from the acquired source")
            location["excerptSha256"] = actual_hash
            location["locator"] = block["locator"]
        link.setdefault("confidenceLabel", "not-confident")
        _upsert(graph["verificationLinks"], link, "id")
    for field, key in (("searchRuns", "id"), ("completenessAssessments", "claimId"),
                       ("reviewAssertions", "id")):
        for value in record.get(field, []):
            _upsert(graph[field], value, key)
    _refresh_processed(graph)
    validate_audit_graph(graph)
    state["bindingSha256"] = _binding_digest(graph)
    # Immutable history retains earlier abstract/full-text readings even when
    # the active graph edge is replaced. A duplicate record is idempotent.
    history = {
        "schemaVersion": 1, "auditId": state["auditId"],
        "scopeSha256": state["scopeSha256"], "record": copy.deepcopy(record),
        "source": {key: source[key] for key in ("id", "artifactSha256", "textSha256", "accessLevel")}
        if source else None,
    }
    history_id = digest(history)
    history_path = _path(workspace, f".ocsl-review/literature/history/{history_id}.json")
    if history_path.exists():
        if history_path.stat().st_nlink != 1 or _read_json(history_path) != history:
            raise ValidationError("immutable assessment history changed or shares inodes")
    else:
        atomic_json(history_path, history, mode=0o600)


def cmd_record(args: Any) -> dict[str, Any]:
    workspace = _workspace(args)
    with _locked(workspace):
        state, graph = _load(args, workspace)
        record = _read_json(_path(workspace, args.input, exists=True))
        record_assessments(workspace, state, graph, record)
        _save(workspace, state, graph, event="assessments-recorded")
        return literature_status(state, graph)


def guard_graph(workspace: Path, graph: Mapping[str, Any]) -> None:
    """Reject stale graph projections and evidence before publishing conclusions."""
    ledger = _path(workspace, LEDGER_PATH)
    if not ledger.exists():
        if graph.get("sourceExaminations"):
            raise ValidationError("source examination provenance requires the managed literature ledger")
        return
    state = _read_json(ledger)
    _validate_state(state)
    if state["auditId"] != graph.get("auditId") or state["graphSha256"] != digest(graph):
        raise ValidationError("audit graph is stale relative to the literature ledger; resume or prepare it again")
    manuscript = _read_json(_path(workspace, state["manuscriptPath"], exists=True))
    if digest(_scope(manuscript, graph)) != state["scopeSha256"]:
        raise ValidationError("literature confirmation belongs to an older manuscript or bibliography")
    for source_id in {e["sourceId"] for e in state["entries"] if e["sourceId"]}:
        _verified_source(workspace, state["sources"][source_id])
