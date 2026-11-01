# SPDX-License-Identifier: Apache-2.0
"""Statistics-report table sources for the deterministic numeric comparison (``followup numeric --tables``).

A table source implements ``followup_model.TableSource``: it names the report and edition a cited work is
(``report_for``), lists a report's editions and yields the cells of one edition (condition, metric, geography,
reference year, value, unit, locator). Sources are pluggable: a licensed table set replaces the synthetic fixtures
without code changes, as long as it is a file of a supported format. ``JsonTableSource`` reads format
``ocsl-followup-tables-v1``.

A source declares its ``licensing``: ``synthetic`` (test fixtures), ``unverified`` (never ingested) or ``verified``.
The numeric command refuses an unverified source, accepts a synthetic one only behind an explicit test flag, and
compares the file's sha256 with the one the protocol pins. Nothing of a source is copied into the branch except the
cells a comparison matched.

Matching of condition, metric and geography is by folded text (case, spacing, typographic punctuation and a leading
article ignored) mapped through versioned synonym lists: a small built-in list (``table-synonyms@1``) and the
source's own ``synonyms`` block, which extends or overrides it. The built-in list is domain-neutral (the condition
"all" and common geography spellings); labels of a particular field (for example the ways a report names all of its
conditions together) belong in the source's block.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ValidationError
from .followup_model import TableCell, WorkRef, dataset_id
from .retrieval import normalise_identifier

FORMAT = "ocsl-followup-tables-v1"
LICENSING = ("synthetic", "unverified", "verified")
KINDS = ("condition", "metric", "geography")
MAX_BYTES = 64 * 1024 * 1024
BUILTIN_SYNONYMS_VERSION = "table-synonyms@1"
# Domain-neutral entries only: condition labels of a particular field belong in the table source's own synonyms block.
BUILTIN_SYNONYMS: dict[str, dict[str, tuple[str, ...]]] = {
    "condition": {"all": ("all diseases", "all conditions")},
    "metric": {},
    "geography": {"world": ("worldwide", "global", "globally", "the world"),
                  "united states": ("usa", "us", "u.s.", "u.s.a.", "united states of america"),
                  "united kingdom": ("uk", "u.k.", "great britain")},
}
TOP_KEYS = {"format", "name", "license", "licensing", "source", "declaredVersion", "synonyms", "reports"}
REPORT_KEYS = {"reportKey", "title", "identifiers", "editions"}
EDITION_KEYS = {"citedAs", "cells", "year"}
CELL_KEYS = {"condition", "metric", "geography", "year", "value", "unit", "locator"}


def fold(text: Any) -> str:
    """Comparison form of a label: casefolded, typographic punctuation normalised, spacing collapsed, edges trimmed of
    punctuation, and a leading article dropped."""
    value = str(text or "")
    for old, new in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'), ("–", "-"), ("—", "-"), ("\u00a0", " ")):
        value = value.replace(old, new)
    value = " ".join(value.casefold().split()).strip(" .,;:!?\"'()[]")
    return value[4:] if value.startswith("the ") else value


def _title(text: Any) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", str(text or "").casefold()).split())


def _strings(value: Any, where: str) -> list[str]:
    values = value if isinstance(value, list) else [value] if isinstance(value, str) else None
    if values is None or not all(isinstance(item, str) and item.strip() for item in values):
        raise ValidationError(f"table source: {where} must be a string or a list of strings")
    return values


class JsonTableSource:
    """A table source in one JSON file (format ``ocsl-followup-tables-v1``); see the module docstring."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise ValidationError(f"table source is missing: {self.path}")
        data = self.path.read_bytes()
        if len(data) > MAX_BYTES:
            raise ValidationError("table source exceeds the size bound")
        self.sha256 = hashlib.sha256(data).hexdigest()
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise ValidationError("table source is not UTF-8 JSON") from error
        if not isinstance(value, dict) or value.get("format") != FORMAT:
            raise ValidationError(f"table source must be a {FORMAT} object")
        unknown = sorted(set(value) - TOP_KEYS)
        if unknown:
            raise ValidationError("table source carries unknown fields: " + ", ".join(unknown))
        self.licensing = value.get("licensing")
        if self.licensing not in LICENSING:
            raise ValidationError(f"table source licensing must be one of {', '.join(LICENSING)}")
        self.declared_version = value.get("declaredVersion")
        if not isinstance(self.declared_version, str) or not self.declared_version.strip():
            raise ValidationError("table source needs its declaredVersion")
        self.name = value.get("name") or "statistics-tables"
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", self.name):
            raise ValidationError("table source name must be a lower-case identifier")
        self.description = str(value.get("source") or "")
        self.source_id = dataset_id(self.name, self.declared_version, self.sha256)
        self.synonyms_version, self._synonyms = self._load_synonyms(value.get("synonyms"))
        self._reports: dict[str, dict[str, Any]] = {}
        reports = value.get("reports")
        if not isinstance(reports, list) or not reports:
            raise ValidationError("table source needs a non-empty reports list")
        for index, report in enumerate(reports):
            self._load_report(report, index)

    # ------------------------------------------------------------------ loading
    def _load_synonyms(self, block: Any) -> tuple[str | None, dict[str, dict[str, str]]]:
        mapping: dict[str, dict[str, str]] = {kind: {} for kind in KINDS}
        for kind, groups in BUILTIN_SYNONYMS.items():
            for canonical_label, variants in groups.items():
                for variant in (canonical_label, *variants):
                    mapping[kind][fold(variant)] = fold(canonical_label)
        if block is None:
            return None, mapping
        if not isinstance(block, dict) or not isinstance(block.get("version"), str) or not block["version"].strip():
            raise ValidationError("table source synonyms need a version and lists per kind")
        unknown = sorted(set(block) - {"version", *KINDS})
        if unknown:
            raise ValidationError("table source synonyms carry unknown kinds: " + ", ".join(unknown))
        for kind in KINDS:
            groups = block.get(kind) or {}
            if not isinstance(groups, dict):
                raise ValidationError(f"table source synonyms.{kind} must map a label to its variants")
            for canonical_label, variants in groups.items():
                for variant in (canonical_label, *_strings(variants, f"synonyms.{kind}.{canonical_label}")):
                    mapping[kind][fold(variant)] = fold(canonical_label)
        return block["version"], mapping

    def _load_report(self, report: Any, index: int) -> None:
        if not isinstance(report, dict) or not isinstance(report.get("reportKey"), str) or not report["reportKey"]:
            raise ValidationError(f"table source report {index} needs a reportKey")
        key = report["reportKey"]
        unknown = sorted(set(report) - REPORT_KEYS)
        if unknown:
            raise ValidationError(f"table source report {key} carries unknown fields: " + ", ".join(unknown))
        if key in self._reports:
            raise ValidationError(f"table source report {key} is declared twice")
        editions = report.get("editions")
        if not isinstance(editions, dict) or not editions:
            raise ValidationError(f"table source report {key} needs editions")
        loaded: dict[str, dict[str, Any]] = {}
        for edition, spec in editions.items():
            if not isinstance(spec, dict) or not isinstance(spec.get("cells"), list):
                raise ValidationError(f"table source report {key} edition {edition} needs cells")
            unknown = sorted(set(spec) - EDITION_KEYS)
            if unknown:
                raise ValidationError(f"table source report {key} edition {edition} carries unknown fields: "
                                      + ", ".join(unknown))
            cells = [self._cell(key, edition, cell, position) for position, cell in enumerate(spec["cells"])]
            loaded[edition] = {"citedAs": self._identifiers(spec.get("citedAs") or {}, f"{key}.{edition}.citedAs"),
                               "cells": cells}
        self._reports[key] = {"title": report.get("title"), "editions": loaded,
                              "identifiers": self._identifiers(report.get("identifiers") or {}, f"{key}.identifiers")}

    @staticmethod
    def _identifiers(block: Any, where: str) -> dict[str, set[str]]:
        if not isinstance(block, dict):
            raise ValidationError(f"table source {where} must be an object")
        unknown = sorted(set(block) - {"doi", "pmid", "title"})
        if unknown:
            raise ValidationError(f"table source {where} carries unknown identifiers: " + ", ".join(unknown))
        result: dict[str, set[str]] = {}
        for scheme, values in block.items():
            normalised = {_title(item) if scheme == "title" else normalise_identifier(scheme, item)
                          for item in _strings(values, f"{where}.{scheme}")}
            if "" in normalised:
                raise ValidationError(f"table source {where}.{scheme} holds an invalid identifier")
            result[scheme] = normalised
        return result

    @staticmethod
    def _cell(report: str, edition: str, cell: Any, position: int) -> TableCell:
        where = f"report {report} edition {edition} cell {position}"
        if not isinstance(cell, dict) or set(cell) - CELL_KEYS:
            raise ValidationError(f"table source {where} must be an object with {', '.join(sorted(CELL_KEYS))}")
        for name in ("condition", "metric", "geography", "unit"):
            if not isinstance(cell.get(name), str):
                raise ValidationError(f"table source {where} needs a string {name}")
        value, year = cell.get("value"), cell.get("year")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValidationError(f"table source {where} needs a finite number value")
        if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
            raise ValidationError(f"table source {where} year must be an integer or null")
        return TableCell(report, edition, cell["condition"], cell["metric"], cell["geography"], year, value,
                         cell["unit"], str(cell.get("locator") or ""))

    # ------------------------------------------------------------------ TableSource
    def describe(self) -> dict[str, Any]:
        return {"format": FORMAT, "sourceId": self.source_id, "name": self.name, "source": self.description,
                "declaredVersion": self.declared_version, "licensing": self.licensing, "sha256": self.sha256,
                "reports": len(self._reports),
                "editions": sum(len(report["editions"]) for report in self._reports.values()),
                "cells": sum(len(edition["cells"]) for report in self._reports.values()
                             for edition in report["editions"].values()),
                "synonyms": {"builtin": BUILTIN_SYNONYMS_VERSION, "source": self.synonyms_version}}

    def report_for(self, work: WorkRef) -> tuple[str, str] | None:
        """(report key, edition) of a cited work by DOI, PMID or exact title; edition "" when only the report is known."""
        doi = normalise_identifier("doi", work.doi or "")
        pmid = normalise_identifier("pmid", work.pmid or "")
        title = _title(work.title)

        def matches(identifiers: Mapping[str, set[str]], *, by_title: bool) -> bool:
            if doi and doi in identifiers.get("doi", set()):
                return True
            if pmid and pmid in identifiers.get("pmid", set()):
                return True
            return by_title and bool(title) and title in identifiers.get("title", set())
        for by_title in (False, True):
            for key in sorted(self._reports):
                report = self._reports[key]
                for edition in report["editions"]:
                    if matches(report["editions"][edition]["citedAs"], by_title=by_title):
                        return key, edition
                if matches(report["identifiers"], by_title=by_title):
                    return key, ""
        return None

    def editions(self, report_key: str) -> Sequence[str]:
        return list(self._reports[report_key]["editions"]) if report_key in self._reports else []

    def cells(self, report_key: str, edition: str) -> Sequence[TableCell]:
        report = self._reports.get(report_key)
        if not report or edition not in report["editions"]:
            return []
        return list(report["editions"][edition]["cells"])

    def canonical(self, kind: str, text: Any) -> str:
        """The comparison label of a condition, metric or geography (folded, then mapped through the synonyms)."""
        if kind not in KINDS:
            raise ValidationError(f"unknown label kind: {kind}")
        folded = fold(text)
        return self._synonyms[kind].get(folded, folded)


def load(path: Path) -> JsonTableSource:
    """The table source in ``path`` (the only supported format is ocsl-followup-tables-v1)."""
    return JsonTableSource(Path(path))
