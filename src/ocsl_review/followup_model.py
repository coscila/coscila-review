# SPDX-License-Identifier: Apache-2.0
"""Data model and interfaces of the follow-up audit (pure Python, no I/O).

The follow-up asks which claims of the review articles in a frozen census rest on cited works with a dated
retraction, expression of concern or correction, or on works that the census shows to be cited systematically
without support, and how such claims propagate to later articles. This module holds the identifiers, records and
protocols the follow-up modules exchange; storage is ``followup_schema`` and ``followup_branch``.

Interfaces:
- ``StatusSource``: one status source (Crossref, the Retraction Watch CSV snapshot, PubMed, Europe PMC) observing
  one cited work; a source that cannot answer returns an ``unknown`` observation instead of raising.
- ``CitedBySource``: works citing one article (Europe PMC citations, OpenAlex); an unavailable service is a recorded
  state, never an exception.
- ``TableSource``: statistics-report tables for the deterministic numeric comparison; pluggable, so a licensed table
  set replaces the synthetic fixtures without code changes.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .followup_schema import NOTICE_KINDS, OBSERVED_STATUSES, STATUS_SOURCES

INSTRUMENTS = {
    "support": "citation-support@2",
    "function": "citation-function@1",
    "numeric": "numeric-check@1",
    "match": "claim-match@1",
}
FOLLOWUP_INSTRUMENTS = ("citation-function@1", "claim-match@1", "numeric-check@1")
# The support instruments a protocol may pin (instruments.support): citation-support@2 unchanged, which declares no
# request settings and runs only on a judge called directly, or its verbatim version, which carries citation-support@2's
# prompt, output schema, validator and class map with its request settings declared per tier and one item per call, so
# the run principal's broker can check and send every call unchanged.
VERBATIM_SUPPORT_INSTRUMENT = "citation-support-verbatim@1"
SUPPORT_INSTRUMENTS = ("citation-support@2", VERBATIM_SUPPORT_INSTRUMENT)
# The instruments the broker judge runs: every follow-up instrument that exports verbatim broker bundles.
BROKER_INSTRUMENTS = (*FOLLOWUP_INSTRUMENTS, VERBATIM_SUPPORT_INSTRUMENT)
STATUS_RULE = "status-resolution@1"
DEPENDENCY_RULE = "dependency-aggregation@1"
NUMERIC_RULE = "numeric-comparison@1"
REDACTED_PREFIX = "sha256:"


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(*parts: Any) -> str:
    return hashlib.sha256(canonical(list(parts)).encode("utf-8")).hexdigest()


# The status markers publishers and indexes add to a retracted, withdrawn, corrected or flagged work's title: a leading
# "RETRACTED:", "Retracted article:", "WITHDRAWN:", "Retraction note:", "Notice of retraction:", "Expression of
# concern:", "Correction to:", "Erratum:" or "Corrigendum:", and a trailing "[Retracted]" or "(Retracted article. See
# ...)". The judges never see a cited work's status, so every cited title leaves without them, in every stratum alike.
_TITLE_STATUS_LEAD = re.compile(
    r"^\s*(?:(?:retracted|withdrawn)(?:\s+(?:article|paper|publication|chapter))?"
    r"|(?:notice\s+of\s+)?(?:retraction|withdrawal)(?:\s+(?:note|notice|statement))?(?:\s+(?:to|of|for))?"
    r"|(?:editorial\s+)?(?:expression|notice)\s+of\s+concern(?:\s+(?:to|for|regarding|about|on))?"
    r"|(?:correction|erratum|corrigendum)(?:\s+(?:to|of|for))?)\s*[:\uff1a.\-\u2013\u2014]+\s*",
    re.IGNORECASE)
_TITLE_STATUS_TRAIL = re.compile(
    r"\s*[\[(][^\[\]()]*\b(?:retract\w*|withdrawn|withdrawal|expression\s+of\s+concern|erratum|corrigendum|correction)"
    r"\b[^\[\]()]*[\])]\s*\.?\s*$", re.IGNORECASE)


def status_blind_title(title: Any) -> str:
    """A cited work's title as a judge may see it: without invisible format characters and without the status markers
    above (repeatedly, so "RETRACTED: Retraction note: ..." loses both)."""
    text = "".join(character for character in str(title or "") if unicodedata.category(character) != "Cf").strip()
    while True:
        stripped = _TITLE_STATUS_TRAIL.sub("", _TITLE_STATUS_LEAD.sub("", text, count=1), count=1).strip()
        if stripped == text:
            return text
        text = stripped


def locus_id(journal: str, doi: str, label: str, seq: int) -> str:
    """The follow-up id of a parent census locus (one marker occurrence of one bibliography entry)."""
    return "locus-" + digest("locus", journal, doi, label, int(seq))[:32]


def sentence_digest(sentence: str) -> str:
    """sha256 of a citing sentence; a frozen census stores entitled sentences as ``sha256:<hex>`` of the text."""
    if sentence.startswith(REDACTED_PREFIX) and len(sentence) == len(REDACTED_PREFIX) + 64:
        return sentence[len(REDACTED_PREFIX):]
    return hashlib.sha256(sentence.encode("utf-8")).hexdigest()


def claim_id(journal: str, doi: str, unit: int, sentence_sha256: str) -> str:
    """One citing sentence of one review: every locus in the sentence belongs to this claim."""
    return "claim-" + digest("claim", journal, doi, int(unit), sentence_sha256)[:32]


def dataset_id(name: str, declared_version: str, sha256: str) -> str:
    return f"{name}@{declared_version}#{sha256[:12]}"


@dataclass(frozen=True)
class WorkRef:
    """A cited work of the parent census, as the status and cited-by sources see it (identifiers only)."""
    work_key: str
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    title: str | None = None
    year: str | None = None


@dataclass(frozen=True)
class StatusObservation:
    """One dated observation per (work, source, status, notice DOI, notice date, retrieved_at)."""
    work_key: str
    source: str
    status: str
    retrieved_at: str
    snapshot_id: str
    covers: tuple[str, ...]
    notice_type: str | None = None
    notice_doi: str | None = None
    notice_pmid: str | None = None
    notice_date: str | None = None
    date_precision: str | None = None
    receipt_sha256: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source not in STATUS_SOURCES:
            raise ValueError(f"unknown status source: {self.source}")
        if self.status not in OBSERVED_STATUSES:
            raise ValueError(f"unknown status: {self.status}")
        if not set(self.covers) <= set(NOTICE_KINDS):
            raise ValueError(f"unknown notice kinds covered: {sorted(set(self.covers) - set(NOTICE_KINDS))}")
        if self.status in ("none", "unknown") and (self.notice_doi or self.notice_date):
            raise ValueError("a none or unknown observation carries no notice")

    @property
    def observation_id(self) -> str:
        return "obs-" + digest(self.work_key, self.source, self.status, self.notice_doi or "", self.notice_date or "",
                               self.retrieved_at)[:40]

    def row(self) -> tuple:
        return (self.observation_id, self.work_key, self.source, self.status, self.notice_type, self.notice_doi,
                self.notice_pmid, self.notice_date, self.date_precision, canonical(sorted(self.covers)),
                self.retrieved_at, self.snapshot_id, self.receipt_sha256, canonical(dict(self.detail)))


@dataclass(frozen=True)
class WorkStatus:
    """The one status row every cited work gets (status-resolution rule)."""
    work_key: str
    status: str
    sources: Sequence[Mapping[str, Any]]
    checked_at: str
    snapshot_id: str
    rule_version: str = STATUS_RULE
    reinstated: bool = False
    notice_doi: str | None = None
    notice_date: str | None = None
    date_precision: str | None = None
    decided_by: str | None = None
    unknown_kinds: Sequence[str] = ()


@runtime_checkable
class StatusSource(Protocol):
    """A status source; ``observe`` returns at least one observation and never raises for a provider failure."""
    name: str
    covers: tuple[str, ...]

    def observe(self, work: WorkRef, *, snapshot_id: str) -> list[StatusObservation]:
        ...


@dataclass(frozen=True)
class TargetRef:
    """An article whose citing works are traversed (an affected review)."""
    doi: str
    journal: str | None = None
    pmid: str | None = None
    pmcid: str | None = None


@dataclass(frozen=True)
class CitedByEdge:
    target_doi: str
    citing_key: str
    source: str
    request_id: str
    retrieved_at: str
    citing_doi: str | None = None
    citing_pmid: str | None = None
    citing_pmcid: str | None = None
    citing_openalex: str | None = None
    citing_title: str | None = None
    citing_year: int | None = None
    citing_first_online: str | None = None

    @property
    def edge_id(self) -> str:
        return "edge-" + digest(self.target_doi, self.source, self.citing_key)[:32]


@dataclass
class CitedByResult:
    """Outcome of one traversal: ``state`` is complete, truncated, unavailable, error or no-identifier."""
    target: TargetRef
    source: str
    state: str
    edges: list[CitedByEdge] = field(default_factory=list)
    hit_count: int | None = None
    reason: str | None = None


@runtime_checkable
class CitedBySource(Protocol):
    name: str

    def citing(self, target: TargetRef, *, limit: int | None = None) -> CitedByResult:
        ...


@dataclass(frozen=True)
class TableCell:
    """One statistic of a statistics report edition, as a table source holds it."""
    report_key: str
    edition: str
    condition: str
    metric: str
    geography: str
    year: int | None
    value: float
    unit: str
    locator: str = ""


@runtime_checkable
class TableSource(Protocol):
    """Statistics-report tables. ``licensing`` is verified, unverified or synthetic; numeric checks of a study run
    only on a verified source, and a source whose licensing is unverified is never ingested."""
    source_id: str
    licensing: str

    def describe(self) -> Mapping[str, Any]:
        ...

    def report_for(self, work: WorkRef) -> tuple[str, str] | None:
        """(report key, edition) of a cited work (None: the work is no report this source holds)."""
        ...

    def editions(self, report_key: str) -> Sequence[str]:
        ...

    def cells(self, report_key: str, edition: str) -> Sequence[TableCell]:
        ...


@dataclass(frozen=True)
class FrameUnit:
    """A follow-up locus: one marker occurrence of one bibliography entry of one review, with its cited work."""
    locus_id: str
    journal: str
    doi: str
    label: str
    seq: int
    work_key: str | None
    claim_id: str
    sentence_sha256: str
    provenance_class: str
