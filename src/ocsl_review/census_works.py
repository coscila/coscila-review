# SPDX-License-Identifier: Apache-2.0
"""Cited-work records derived from a review's own bibliography (``study works``).

A fresh census derives its cited works instead of carrying them over from an
earlier study: every printed entry's identifiers (a DOI or PMID in the
bibliography markup, else a DOI printed in the entry) become one work record per
cited work, keyed by its DOI (else its PMID), bound to the digest of the document
they were derived from. Entries without an identifier get no work and stay
unresolved. The metadata a judge sees (title, venue, year, PMCID, retraction
flag) is filled afterwards by ``study abstracts`` from the same Europe PMC
record as the abstract, with its provenance, so nothing about a cited work
comes from an earlier manifest. ``study prepare --require-fresh-enumeration``
derives the works again from the document and refuses a record whose works
differ.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree as ET

from .errors import ValidationError

WORKS_DERIVATION = "bibliography-identifiers@1"
METADATA_FIELDS = ("title", "venue", "year", "pmcid", "retracted")


def _entry_identifiers(entry: Any) -> dict[str, str]:
    from .census_extraction import printed_doi
    doi = str(entry.identifiers.get("doi") or "").strip().lower() or printed_doi(entry.printed)
    pmid = str(entry.identifiers.get("pmid") or "").strip()
    return {key: value for key, value in (("doi", doi), ("pmid", pmid)) if value}


def derive_works(document: bytes, kind: str, table_pages: Sequence[tuple[int, bytes]] = ()) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """(works, counts) from a review document's bibliography; the extraction's refusals propagate.

    A work is keyed ``doi:<doi>``, else ``pmid:<pmid>``. A PMID printed beside one DOI belongs to that DOI's work;
    a PMID printed beside several DOIs belongs to none of them, and entries citing it alone get a work of their own.
    """
    from .census_extraction import extract_review
    if kind not in ("jats", "html"):
        raise ValidationError("document kind must be jats or html")
    review = extract_review(document, html=kind == "html", table_pages=table_pages)
    dois: dict[str, set[str]] = {}
    pmid_only: set[str] = set()
    without = 0
    for entry in review["entries"]:
        identifiers = _entry_identifiers(entry)
        if identifiers.get("doi"):
            dois.setdefault(identifiers["doi"], set()).update([identifiers["pmid"]] if identifiers.get("pmid") else [])
        elif identifiers.get("pmid"):
            pmid_only.add(identifiers["pmid"])
        else:
            without += 1
    owners: dict[str, set[str]] = {}
    for doi, pmids in dois.items():
        for pmid in pmids:
            owners.setdefault(pmid, set()).add(doi)
    works: dict[str, dict[str, Any]] = {}
    for doi in sorted(dois):
        pmids = sorted(p for p in dois[doi] if owners[p] == {doi})
        works["doi:" + doi] = {"doi": doi, **({"pmid": pmids[0]} if len(pmids) == 1 else {})}
    for pmid in sorted(pmid_only):
        if owners.get(pmid) and len(owners[pmid]) == 1:
            continue            # the work of the DOI printed beside it carries it
        works["pmid:" + pmid] = {"pmid": pmid}
    return works, {"entries": len(review["entries"]), "withoutIdentifier": without, "works": len(works)}


def derived_record(record: Mapping[str, Any], document: bytes, kind: str,
                   table_pages: Sequence[tuple[int, bytes]] = ()) -> dict[str, Any]:
    """The record with its works derived from ``document``; an unextractable document leaves no works."""
    digest = hashlib.sha256(document).hexdigest()
    out = dict(record)
    try:
        works, counts = derive_works(document, kind, table_pages)
    except (ValidationError, ET.ParseError, UnicodeError) as error:
        out["works"] = {}
        out["worksDerivation"] = {"derivation": WORKS_DERIVATION, "documentSha256": digest, "state": "extraction-failed",
                                  "reason": f"{type(error).__name__}: {str(error)[:300]}"}
        return out
    provenance = {"derivation": WORKS_DERIVATION, "documentSha256": digest}
    out["works"] = {key: {**work, "workProvenance": dict(provenance)} for key, work in works.items()}
    out["worksDerivation"] = {**provenance, "state": "derived", **counts}
    return out


def works_mismatch(record: Mapping[str, Any], document: bytes, kind: str,
                   table_pages: Sequence[tuple[int, bytes]] = ()) -> str | None:
    """Why a record's works are not the ones derived from its document (None when they are, or when the document
    cannot be extracted, which preparation records as an extraction failure)."""
    try:
        derived, _ = derive_works(document, kind, table_pages)
    except (ValidationError, ET.ParseError, UnicodeError):
        return None
    works = record.get("works") if isinstance(record.get("works"), dict) else {}
    if set(works) != set(derived):
        return ("the record's cited works are not those derived from its own bibliography (run study works on the "
                "acquired records; works are never carried over)")
    digest = hashlib.sha256(document).hexdigest()
    for key, work in works.items():
        provenance = work.get("workProvenance") if isinstance(work, dict) else None
        if not isinstance(provenance, dict) or provenance.get("derivation") != WORKS_DERIVATION \
                or provenance.get("documentSha256") != digest:
            return f"work {key} carries no derivation from this document"
        for name in ("doi", "pmid"):
            if (work.get(name) or None) != (derived[key].get(name) or None):
                return f"work {key} changed its {name} after the derivation"
    return None


def metadata_digest(work: Mapping[str, Any]) -> str:
    """Digest of the metadata a judge sees about a work (and its PMCID), which a metadata provenance record binds."""
    values = [work.get(name) for name in METADATA_FIELDS]
    values[2] = None if values[2] is None else str(values[2])
    values[4] = bool(values[4])
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def metadata_from_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Title, venue, year, PMCID and retraction flag of a Europe PMC core record."""
    from .census_inputs import normal_pmcid
    from .scholarly import normalize_europe_pmc
    item = normalize_europe_pmc(raw)
    return {"title": item.get("title") or None, "venue": item.get("container") or None, "year": item.get("year"),
            "pmcid": normal_pmcid(item.get("pmcid")), "retracted": bool(item.get("isRetracted"))}


def apply_metadata(work: dict[str, Any], raw: Mapping[str, Any] | None, provenance: Mapping[str, Any]) -> None:
    """Fill a derived work's metadata from the Europe PMC record its lookup selected (none: unavailable)."""
    if raw is not None:
        work.update(metadata_from_record(raw))
    else:
        work.update({"title": None, "venue": None, "year": None, "pmcid": None, "retracted": False})
    work["metadataProvenance"] = {**dict(provenance), "fieldsSha256": metadata_digest(work)}


def metadata_label(work: Mapping[str, Any]) -> str | None:
    """The provenance class of a work's metadata: the recorded lookup's, or supplied when the fields changed since
    (or a record without provenance supplied them); None for a supplied work without metadata provenance."""
    from .scores_db import PROVENANCE
    recorded = work.get("metadataProvenance")
    if isinstance(recorded, dict) and recorded.get("provenance") in PROVENANCE:
        return str(recorded["provenance"]) if recorded.get("fieldsSha256") == metadata_digest(work) else "supplied"
    if isinstance(work.get("workProvenance"), dict):
        return "supplied"
    return None


def fill_works(source: Path, output: Path) -> dict[str, Any]:
    """Stream review records into ``<output>.partial`` with works derived from their documents; resume; rename.

    Records without a document (unavailable) or awaiting acquisition pass through. A record that already carries
    works is refused: a fresh study derives them, it never carries them over.
    """
    from .census_runner import iter_records, read_document, read_table_pages
    from .census_inputs import _resume_partial
    source, output = Path(source), Path(output)
    if output.resolve() == source.resolve():
        raise ValidationError("write the records with derived works to a new file")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(output) + ".partial")
    done = _resume_partial(partial)
    counts = {"records": 0, "resumed": len(done), "derived": 0, "passedThrough": 0, "extractionFailed": 0, "works": 0,
              "entriesWithoutIdentifier": 0}
    with partial.open("a", encoding="utf-8") as handle:
        for index, record in enumerate(iter_records(source)):
            if index < len(done):
                if (record.get("journal"), record.get("doi")) != done[index]:
                    raise ValidationError(f"{partial} does not continue this input (record {index + 1} differs)")
                counts["records"] += 1
                continue
            if record.get("works"):
                raise ValidationError(f"record {index + 1} ({record.get('doi')}) already carries works; study works "
                                      "derives them from the review's own bibliography")
            acquisition = record.get("acquisition") if isinstance(record.get("acquisition"), dict) else {}
            document = None if acquisition.get("state") == "retry" else read_document(source.parent, record)
            if document is None:
                result = dict(record)
                counts["passedThrough"] += 1
            else:
                kind = (record.get("document") or {}).get("kind")
                result = derived_record(record, document, kind, read_table_pages(source.parent, record))
                derivation = result["worksDerivation"]
                if derivation["state"] == "derived":
                    counts["derived"] += 1
                    counts["works"] += derivation["works"]
                    counts["entriesWithoutIdentifier"] += derivation["withoutIdentifier"]
                else:
                    counts["extractionFailed"] += 1
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            handle.flush()
            counts["records"] += 1
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, output)
    return {**counts, "output": str(output), "derivation": WORKS_DERIVATION,
            "next": "study abstracts fills each derived work's abstract and metadata from Europe PMC"}
