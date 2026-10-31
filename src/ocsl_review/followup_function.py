# SPDX-License-Identifier: Apache-2.0
"""citation-function@1 on the loci of the frozen strata, and the ``fu_function`` rows.

The judge sees, per locus, the named work's marker as rendered in the citing sentence, the citing sentence, the
sentences before and after it in the same citing unit, the section and the cited work's title and year; never the
work's status, the stratum, support verdicts, identifiers or the journal (so S0 and S1 loci are judged alike).

Neighbouring sentences: the parent stores only the citing sentence. With ``--documents`` (the census's review
documents, read-only, named by their sha256 as the census stored them) the review is re-extracted with the census's
own extraction, the citing unit whose sentence digest matches is found and its neighbours are taken; otherwise they are
empty strings, as the instrument's evidence policy says.

This module also holds the unit selection the support and function commands share (strata, the pilot subsample, the
replicate units of ``followup_strata.replicate_keys``, which every judging command uses), the marker of a locus, the
review documents and the judge checks.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ValidationError
from .followup_branch import FollowupBranch, require_freeze
from .followup_judge import close_judge, judge_summary, prepare_judge
from .followup_model import sentence_digest
from .followup_schema import UNASSESSABLE

INSTRUMENT_ID = "citation-function@1"
TIER = "locus"
SCOPE_STRATA = ("S0", "S1", "S2", "S3")
MARK = re.compile(r"⟨([^ ⟩]+)⟩")


# ----------------------------------------------------------------------------- unit selection
def select_units(connection: Any, strata: Sequence[str], *, scope: str = "all", variant: str = "primary",
                 protocol: Mapping[str, Any] | None = None, include_cocited_of_pilot: bool = False,
                 instrument_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Frame units of the given strata (document order), restricted to the pilot or drawn for the replicate.

    ``include_cocited_of_pilot`` adds, in the pilot scope, the co-cited loci of pilot anchors (the dependency of a
    pilot anchor needs their verdicts). The replicate units are ``followup_strata.replicate_keys`` of the units in
    scope with the instrument id as its scope, as for every judging command.
    """
    if scope not in ("all", "pilot"):
        raise ValidationError("--scope is pilot or all")
    if variant not in ("primary", "replicate"):
        raise ValidationError("--variant is primary or replicate")
    marks = ",".join("?" for _ in strata)
    rows = [dict(row) for row in connection.execute(
        "SELECT f.*, (SELECT group_concat(s.stratum, ',') FROM fu_strata s WHERE s.locus_id=f.locus_id AND s.stratum "
        f"IN ({marks})) AS strata, (SELECT MAX(s.pilot) FROM fu_strata s WHERE s.locus_id=f.locus_id AND s.stratum IN "
        f"({marks})) AS pilot FROM fu_frame f WHERE EXISTS (SELECT 1 FROM fu_strata s WHERE s.locus_id=f.locus_id AND "
        f"s.stratum IN ({marks})) ORDER BY f.journal, f.doi, f.unit, f.label, f.seq", (*strata, *strata, *strata))]
    selection: dict[str, Any] = {"strata": list(strata), "scope": scope, "variant": variant, "units": len(rows)}
    if scope == "pilot":
        pilots = {row["locus_id"] for row in rows if row["pilot"]}
        if include_cocited_of_pilot and pilots:
            pilot_marks = ",".join("?" for _ in pilots)
            pilots |= {row[0] for row in connection.execute(
                f"SELECT locus_id FROM fu_cocited WHERE anchor_locus_id IN ({pilot_marks})", sorted(pilots))}
        rows = [row for row in rows if row["locus_id"] in pilots]
        selection["pilotUnits"] = len(rows)
    if variant == "replicate":
        from .followup_strata import REPLICATE_RULE, replicate_keys
        share = ((((protocol or {}).get("strata") or {}).get("replicate") or {}).get("share"))
        seed = (protocol or {}).get("seed")
        if share is None or seed is None:
            raise ValidationError("the protocol declares no replicate share and seed")
        if not instrument_id:
            raise ValidationError("a replicate selection needs the instrument id as its scope")
        keep = set(replicate_keys(protocol, [row["locus_id"] for row in rows], scope_parts=(instrument_id,)))
        rows = [row for row in rows if row["locus_id"] in keep]
        selection.update(replicateRule=REPLICATE_RULE, replicateScope=[instrument_id], seed=seed, share=share,
                         replicateUnits=len(rows))
    return rows, selection


def provenance_of(unit: Mapping[str, Any]) -> str:
    """Routing class of a locus's citing text: public reviews are public, everything else counts as entitled."""
    return "public" if unit.get("provenance_class") == "public" else "entitled"


# ----------------------------------------------------------------------------- markers
def sentence_targets(connection: Any, unit: Mapping[str, Any]) -> list[tuple[int, str]]:
    """(bibliography ordinal, target id) of every parent locus of the unit's citing sentence."""
    rows = connection.execute(
        "SELECT c.ordinal, c.target_id, l.sentence FROM census.loci l JOIN census.citations c ON c.journal=l.journal "
        "AND c.doi=l.doi AND c.label=l.label WHERE l.journal=? AND l.doi=? AND l.unit=?",
        (unit["journal"], unit["doi"], int(unit["unit"]))).fetchall()
    return sorted({(int(row[0]), str(row[1] or "")) for row in rows
                   if sentence_digest(row[2] or "") == unit["sentence_sha256"]})


def citation_target(connection: Any, unit: Mapping[str, Any]) -> str | None:
    row = connection.execute("SELECT target_id FROM census.citations WHERE journal=? AND doi=? AND label=?",
                             (unit["journal"], unit["doi"], unit["label"])).fetchone()
    return row[0] if row else None


def cited_work(connection: Any, work_key: str | None) -> tuple[str | None, Any]:
    row = connection.execute("SELECT title, year FROM census.works WHERE work_key=?", (work_key,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def resolve_marker(sentence: str, target: str | None, siblings: Sequence[tuple[int, str]]) -> tuple[str | None, str]:
    """The named work's ``⟨target⟩`` marker as rendered in the sentence, and how it was found.

    The census renders a JATS target as ``⟨rid⟩`` and a publisher-HTML target without its ``ref-`` prefix. A sentence
    with one marker names that one. Otherwise, when the targets of the sentence's loci match none of its markers and
    as many markers are left as unmatched loci, they are paired in bibliography order; anything else is unresolved.
    """
    from .census_extraction import marker_name
    present = list(dict.fromkeys(MARK.findall(sentence or "")))

    def direct(name: str | None) -> str | None:
        for candidate in (name, marker_name(name, html=True) if name else None):
            if candidate and candidate in present:
                return candidate
        return None
    own = direct(target)
    if own:
        return f"⟨{own}⟩", "target"
    if len(present) == 1:
        return f"⟨{present[0]}⟩", "single-marker"
    claimed = {direct(name) for _, name in siblings} - {None}
    free = [name for name in present if name not in claimed]
    unmatched = list(dict.fromkeys(name for _, name in siblings if direct(name) is None))
    if target in unmatched and len(free) == len(unmatched):
        return f"⟨{free[unmatched.index(target)]}⟩", "bibliography-order"
    return None, "unresolved"


# ----------------------------------------------------------------------------- review documents
def review_unit_sentences(body: bytes, *, html: bool) -> list[list[str]]:
    """The sentences of every citing unit of a review, in the census's unit order (``extract_review``'s units)."""
    from . import structured
    from .census_extraction import _render, sentence_spans
    value = structured.extract(body, html=html)
    units = []
    for block in value["blocks"]:
        if block.get("sourceIdentity") or "sourceLocator" not in block:
            continue
        if block.get("kind") == "table":
            for (row, column), citations in sorted(block.get("explicitCellCitations", {}).items()):
                if citations:
                    units.append((block["rows"][row][column], citations))
        elif block.get("explicitCitations"):
            units.append((block["text"], block["explicitCitations"]))
    sentences = []
    for text, citations in units:
        rendered, _ = _render(text, citations, html=html)
        sentences.append([rendered[start:end] for start, end in sentence_spans(rendered)])
    return sentences


class ReviewDocuments:
    """Neighbouring sentences of citing sentences, from the census's review documents (read-only, digest-checked)."""

    def __init__(self, directory: Path | str | None, *, cache: int = 8):
        self.directory = Path(directory).resolve() if directory else None
        if self.directory is not None and not self.directory.is_dir():
            raise ValidationError(f"the review documents directory is missing: {self.directory}")
        self._cache: OrderedDict[tuple[str, str], list[list[str]] | None] = OrderedDict()
        self._size = cache
        self.counts: Counter = Counter()

    def _units(self, connection: Any, journal: str, doi: str) -> list[list[str]] | None:
        key = (journal, doi)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        units = None
        row = connection.execute("SELECT document_sha256, route FROM census.reviews WHERE journal=? AND doi=?",
                                 key).fetchone()
        if row is not None and row[0] and re.fullmatch(r"[0-9a-f]{64}", row[0]):
            html = row[1] == "html"
            path = self.directory / (row[0] + (".html" if html else ".xml"))
            if path.is_file() and not path.is_symlink():
                body = path.read_bytes()
                if hashlib.sha256(body).hexdigest() == row[0]:
                    try:
                        units = review_unit_sentences(body, html=html)
                    except (ValidationError, ValueError, UnicodeError):
                        units = None
        self._cache[key] = units
        if len(self._cache) > self._size:
            self._cache.popitem(last=False)
        return units

    def context(self, connection: Any, unit: Mapping[str, Any]) -> tuple[str, str, str]:
        """(sentence before, sentence after, how found): document, sentence-not-found, no-document or no-documents."""
        if self.directory is None:
            self.counts["no-documents"] += 1
            return "", "", "no-documents"
        units = self._units(connection, unit["journal"], unit["doi"])
        if units is None:
            self.counts["no-document"] += 1
            return "", "", "no-document"
        index = int(unit["unit"])
        order = ([index] if 0 <= index < len(units) else []) + [i for i in range(len(units)) if i != index]
        for position in order:
            sentences = units[position]
            for offset, sentence in enumerate(sentences):
                if hashlib.sha256(sentence.encode("utf-8")).hexdigest() == unit["sentence_sha256"]:
                    self.counts["document"] += 1
                    return (sentences[offset - 1] if offset > 0 else "",
                            sentences[offset + 1] if offset + 1 < len(sentences) else "", "document")
        self.counts["sentence-not-found"] += 1
        return "", "", "sentence-not-found"


# ----------------------------------------------------------------------------- judging helpers
def check_model(model: str | None, protocol: Mapping[str, Any] | None) -> str:
    if not model:
        raise ValidationError("give --model, the served model the protocol pins")
    pinned = ((protocol or {}).get("model") or {}).get("served")
    if pinned and pinned != model:
        raise ValidationError(f"--model {model!r} differs from the served model the protocol pins ({pinned!r})")
    return model


def existing_blocks(connection: Any, digest: str, tier: str, variant: str) -> set[str]:
    return {row[0] for row in connection.execute(
        "SELECT DISTINCT item_key FROM fu_judgments WHERE instrument_digest=? AND tier=? AND variant=? AND "
        "outcome='blocked'", (digest, tier, variant))}


def block_new(connection: Any, instrument: Any, tier: str, blocked: Sequence[tuple[str, str, str]], *,
              variant: str) -> int:
    """Record items never sent, once per instrument digest, tier, variant and item."""
    from .followup_judge import block_items
    done = existing_blocks(connection, instrument.digest, tier, variant)
    fresh = [item for item in blocked if item[0] not in done]
    return block_items(connection, instrument, tier, fresh, variant=variant) if fresh else 0


def judgment_state(connection: Any, digest: str, tier: str, keys: Sequence[str], *, variant: str = "primary"
                   ) -> dict[str, dict[str, Any]]:
    """Per item key: the accepted judgment (id and verdict), or whether only rejections or blocks exist."""
    state: dict[str, dict[str, Any]] = {}
    wanted = set(keys)
    for row in connection.execute(
            "SELECT judgment_id, item_key, outcome, verdict_json, error_code FROM fu_judgments WHERE instrument_digest=? "
            "AND tier=? AND variant=? ORDER BY judgment_id", (digest, tier, variant)):
        if row[1] not in wanted:
            continue
        entry = state.setdefault(row[1], {"accepted": None, "rejected": False, "blocked": None})
        if row[2] == "accepted":
            entry["accepted"] = (row[0], json.loads(row[3]))
        elif row[2] == "rejected":
            entry["rejected"] = True
        elif row[2] == "blocked":
            entry["blocked"] = row[4]
    return state


def reason_of(code: str | None) -> str:
    return code if code in UNASSESSABLE else "not-judged"


def sanitize(value: Any, scope: str) -> str:
    from .sanitize import sanitize_text
    return sanitize_text(str(value or ""), scope=scope).text


# ----------------------------------------------------------------------------- citation-function@1
def function_items(connection: Any, branch: FollowupBranch, units: Sequence[Mapping[str, Any]],
                   documents: ReviewDocuments) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]], Counter]:
    """Items for the units that can be sent, blocked units with their reasons, and how markers were found."""
    from .citation_function import build_item
    items, blocked, methods = [], [], Counter()
    for unit in units:
        key = unit["locus_id"]
        if unit["work_key"] is None:
            blocked.append((key, "identity-unresolved", "the cited work's identity is unresolved"))
            continue
        text, reason = branch.sentence(connection, unit["journal"], unit["doi"], unit["label"], unit["seq"])
        if text is None:
            blocked.append((key, reason or "no-citing-text", "the citing sentence is not available"))
            continue
        sentence = sanitize(text, "citing-sentence")
        marker, method = resolve_marker(sentence, citation_target(connection, unit), sentence_targets(connection, unit))
        methods[method] += 1
        if marker is None:
            blocked.append((key, "not-judged", "the named work's marker is not found in the citing sentence"))
            continue
        before, after, _ = documents.context(connection, unit)
        title, year = cited_work(connection, unit["work_key"])
        items.append(build_item(key, marker=marker, citing_sentence=sentence,
                                context_before=sanitize(before, "context"), context_after=sanitize(after, "context"),
                                section=sanitize(unit.get("section"), "section"),
                                title=sanitize(title, "cited-title"), year=year, provenance=provenance_of(unit)))
    return items, blocked, methods


def function_rows(connection: Any, instrument: Any, units: Sequence[Mapping[str, Any]],
                  reasons: Mapping[str, str]) -> Counter:
    """Write one fu_function row per unit from the primary judgments (class, or why not assessable)."""
    from .citation_function import row as output_row
    keys = [unit["locus_id"] for unit in units]
    state = judgment_state(connection, instrument.digest, TIER, keys)
    counts: Counter = Counter()
    values = []
    for key in keys:
        entry = state.get(key, {})
        if entry.get("accepted"):
            judgment_id, verdict = entry["accepted"]
            fields = output_row(verdict)
            values.append((key, fields["class"], fields["assessable"], fields["reason"], fields["quote"],
                           fields["quote_field"], judgment_id))
            counts[fields["class"] or fields["reason"]] += 1
            continue
        if key in reasons:
            reason = reason_of(reasons[key])
        elif entry.get("blocked"):
            reason = reason_of(entry["blocked"])
        elif entry.get("rejected"):
            reason = "invalid-output"
        else:
            reason = "not-judged"
        values.append((key, None, 0, reason, None, None, None))
        counts[reason] += 1
    connection.executemany("INSERT OR REPLACE INTO fu_function VALUES(?,?,?,?,?,?,?)", values)
    return counts


def run_function(connection: Any, branch: FollowupBranch, judge: Any, policy: Any, *, model: str,
                 scope: str = "all", variant: str = "primary", public_only: bool = False, limit: int | None = None,
                 concurrency: int = 1, transport_retries: int = 0, cooldown: float = 0.0,
                 documents: ReviewDocuments | None = None, code_revision: str | None = None,
                 sleep: Any = None) -> dict[str, Any]:
    from . import instruments
    from .followup_judge import accepted, run_items
    freeze = require_freeze(connection)
    protocol = branch.protocol()
    check_model(model, protocol)
    instrument = instruments.load(INSTRUMENT_ID)
    prepare_judge(judge, instrument, TIER)
    documents = documents or ReviewDocuments(None)
    everything, _ = select_units(connection, SCOPE_STRATA)
    units, selection = select_units(connection, SCOPE_STRATA, scope=scope, variant=variant, protocol=protocol,
                                    instrument_id=instrument.id)
    items, blocked, methods = function_items(connection, branch, units, documents)
    blocks = block_new(connection, instrument, TIER, blocked, variant=variant)
    sendable = [item for item in items if not public_only or item["provenance"] == "public"]
    done = set(accepted(connection, instrument.digest, TIER, variant))
    pending = [item for item in sendable if item["id"] not in done]
    if limit is not None:
        if limit < 1:
            raise ValidationError("--limit must be positive")
        pending = pending[:limit]
    options = {"sleep": sleep} if sleep is not None else {}
    summary = run_items(connection, instrument, pending, judge, tier=TIER, model=model, policy=policy,
                        variant=variant, concurrency=concurrency, code_revision=code_revision,
                        transport_retries=transport_retries, cooldown=cooldown, **options) if pending else {
        "instrument": instrument.id, "digest": instrument.digest, "items": 0, "calls": 0, "accepted": 0,
        "rejected": 0, "failedCalls": 0, "open": 0, "stopped": None}
    rows: Counter = Counter()
    if variant == "primary":
        _, all_blocked, _ = function_items(connection, branch, everything, ReviewDocuments(None))
        rows = function_rows(connection, instrument, everything, {key: code for key, code, _ in all_blocked})
    return {"instrument": instrument.id, "digest": instrument.digest, "freeze": freeze["freeze_id"],
            "selection": selection, "items": len(items), "sent": len(pending),
            "withheldEntitled": len(items) - len(sendable), "blocked": dict(Counter(code for _, code, _ in blocked)),
            "blockedRecorded": blocks, "markers": dict(methods), "contexts": dict(documents.counts), "run": summary,
            "rows": dict(sorted(rows.items())), "judge": judge_summary(judge)}


def cmd_function(args: Any) -> dict[str, Any]:
    from .census_runner import code_revision
    from .followup_judge import judge_from_args
    branch = FollowupBranch(Path(args.database))
    judge, policy = judge_from_args(args)
    try:
        documents = ReviewDocuments(getattr(args, "documents", None))
        with branch.writing() as connection:
            return run_function(connection, branch, judge, policy, model=args.model, scope=args.scope,
                                variant=args.variant, public_only=args.public_only, limit=args.limit,
                                concurrency=args.concurrency, transport_retries=args.transport_retries,
                                cooldown=args.transport_cooldown, documents=documents, code_revision=code_revision())
    finally:
        close_judge(judge)
